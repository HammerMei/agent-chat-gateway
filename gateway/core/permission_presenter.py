"""Permission message formatting — user-facing presentation only.

Separated from domain state so that wording changes, localization, or
platform-specific formatting do not touch the registry or broker logic.
"""

from __future__ import annotations

from .permission_state import PermissionRequest


def format_request_msg(req: PermissionRequest) -> str:
    """Format the initial permission request notification for the chat room."""
    params = ", ".join(
        f"{k}={repr(v)[:60]}" for k, v in req.tool_input.items()
    )
    if len(params) > 200:
        params = params[:197] + "..."
    return (
        f"🔐 **Permission required** `[{req.request_id}]`\n"
        f"**Tool:** `{req.tool_name}`\n"
        f"**Params:** `{params or '(none)'}`\n"
        f"Reply `approve {req.request_id}` or `deny {req.request_id}`"
    )


def format_timeout_msg(req: PermissionRequest) -> str:
    """Format the timeout auto-deny notification for the chat room."""
    return (
        f"⏱️ Permission `{req.request_id}` timed out — **auto-denied**."
    )
