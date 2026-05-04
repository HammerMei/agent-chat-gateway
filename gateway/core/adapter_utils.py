"""Utilities for AgentBackend adapter authors.

Adapters that lack native file-attachment support (e.g. the Claude CLI backend)
can use :func:`build_attachment_prompt` to inject human-readable attachment notes
into the prompt text so the agent knows which local files are available.

Adapters with native attachment support (e.g. opencode's ``-f`` flag) should
pass files through their native mechanism and skip this helper entirely.

Example usage in an adapter's ``send()`` method::

    from gateway.core.adapter_utils import build_attachment_prompt

    async def send(self, session_id, prompt, working_directory, timeout,
                   attachments=None, env=None):
        prompt = build_attachment_prompt(prompt, attachments, working_directory)
        # ... proceed to call subprocess with prompt
"""

from __future__ import annotations

from pathlib import Path

# ── Timestamp utilities ───────────────────────────────────────────────────────


def ts_to_float(ts: str | None) -> float | None:
    """Convert a timestamp string to a numeric value for reliable ordering.

    Connector timestamps are typically Unix-epoch milliseconds
    (e.g. ``"1711234567890"``).  Falls back to ``None`` when the string
    cannot be parsed so callers can skip comparisons gracefully rather than
    producing false positives/negatives from lexicographic ordering.

    Used by both the core layer (``watcher_lifecycle``) and the RC connector
    (``normalize``) — single source of truth to avoid duplicated logic.
    """
    if not ts:
        return None
    try:
        return float(ts)
    except (ValueError, TypeError):
        return None


def ts_ms_to_iso_local(ts_ms_str: str | None, tz_name: str) -> str | None:
    """Convert a Unix-epoch-millisecond timestamp string to ISO 8601 with local offset.

    Args:
        ts_ms_str: Timestamp string in Unix epoch milliseconds (e.g. ``"1711234567890"``).
                   Returns ``None`` when the string is absent or unparseable.
        tz_name:   IANA timezone name (e.g. ``"America/Los_Angeles"``).
                   The result carries the UTC offset for that zone so agents can
                   read the local time directly without knowing the offset themselves.

    Returns:
        ISO 8601 string with offset, e.g. ``"2026-04-24T10:30:00-07:00"``,
        or ``None`` when ``ts_ms_str`` cannot be parsed.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    f = ts_to_float(ts_ms_str)
    if f is None:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return None
    dt = datetime.fromtimestamp(f / 1000.0, tz=tz)
    return dt.isoformat(timespec="seconds")


def ts_gt(a: str, b: str) -> bool:
    """Return True if timestamp string ``a`` is strictly greater than ``b``.

    Uses numeric comparison via :func:`ts_to_float` so ordering is correct
    regardless of string length.  Falls back to lexicographic comparison when
    either value cannot be parsed (e.g. a legacy ISO-8601 string).
    """
    fa = ts_to_float(a)
    fb = ts_to_float(b)
    if fa is not None and fb is not None:
        return fa > fb
    return a > b


def build_attachment_prompt(
    prompt: str,
    attachments: list[str] | None,
    working_directory: str | None = None,
    instruction: str = "use the Read tool to view it",
) -> str:
    """Inject attachment file-path notes into *prompt* and return the result.

    Each attachment becomes a line of the form::

        [Attached: <original_name> → <path> — <instruction>]

    The path is shown relative to *working_directory* when possible, falling
    back to the absolute path when it cannot be made relative.

    Args:
        prompt: The base prompt text (already cleaned / prefix-injected).
        attachments: List of absolute local file paths, or ``None`` / empty list
            when there are no attachments.  When empty/None the prompt is
            returned unchanged.
        working_directory: Optional base directory used to compute a shorter
            relative path for display.  Has no effect on which file is read.
        instruction: The hint appended after the arrow so the agent knows what
            to do with the file.  Defaults to ``"use the Read tool to view it"``.

    Returns:
        The original *prompt* with attachment notes appended, or *prompt*
        unmodified when *attachments* is empty or ``None``.
    """
    if not attachments:
        return prompt

    notes: list[str] = []
    for path_str in attachments:
        p = Path(path_str)
        if working_directory:
            try:
                label = str(p.relative_to(working_directory))
            except ValueError:
                label = path_str
        else:
            label = path_str

        notes.append(f"[Attached: {p.name} → {label} — {instruction}]")

    return (prompt + "\n" + "\n".join(notes)).strip()
