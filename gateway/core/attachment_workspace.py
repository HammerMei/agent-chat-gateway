"""AttachmentWorkspace: per-watcher symlink management for cached attachments.

Extracted from WatcherLifecycle to keep filesystem preparation separate from
watcher orchestration.  The workspace creates a symlink inside the agent's
working directory that points to the connector's global attachment cache, so
the agent sees attachment files as cwd-local paths (avoiding out-of-project
permission prompts from Claude Code).

Layout::

    {working_directory}/.acg-attachments/{watcher_name}
        → {global_cache}/{connector_name}/{room_id}/
"""

from __future__ import annotations

import logging
from pathlib import Path

from .connector import Attachment, Connector

logger = logging.getLogger("agent-chat-gateway.core.attachment_workspace")


def localize_attachment_paths(
    attachments: list[Attachment],
    local_base: str | None = None,
) -> list[str]:
    """Remap attachment paths through a per-watcher symlink directory.

    If ``local_base`` is set (e.g. ``{cwd}/.acg-attachments/{watcher_name}``
    → global cache dir), each attachment's filename is resolved under the
    symlink so the agent sees a cwd-local path.  This avoids out-of-project
    permission prompts from Claude Code.

    Falls back to the original absolute path when no symlink is configured
    or when the remapped path does not exist (download may have been skipped).
    """
    if not local_base:
        return [att.local_path for att in attachments]

    base = Path(local_base)
    result: list[str] = []
    for att in attachments:
        local = base / Path(att.local_path).name
        if local.exists():
            result.append(str(local))
        else:
            result.append(att.local_path)
    return result


class AttachmentWorkspace:
    """Manages per-watcher attachment symlinks inside agent working directories.

    Usage::

        workspace = AttachmentWorkspace(connector)
        local_base = workspace.setup(watcher_name, room_id, working_directory)
        # local_base is either a str path or None if attachments are unsupported
    """

    def __init__(self, connector: Connector) -> None:
        self._connector = connector

    def setup(
        self,
        watcher_name: str,
        room_id: str,
        working_directory: str,
    ) -> str | None:
        """Create or update a per-watcher symlink for cached attachments.

        Returns:
            Absolute path to the symlink directory (str) if the connector
            supports attachment caching, or ``None`` otherwise.
        """
        cache_dir = self._connector.attachment_cache_dir(room_id)
        if not cache_dir:
            return None

        acg_dir = Path(working_directory) / ".acg-attachments"
        acg_dir.mkdir(parents=True, exist_ok=True)

        link = acg_dir / watcher_name
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)

        if link.is_symlink():
            if link.resolve() != cache_path.resolve():
                link.unlink()
                link.symlink_to(cache_path)
                logger.info("Updated attachment symlink: %s → %s", link, cache_path)
        elif link.exists():
            logger.warning(
                "Attachment path %s exists but is not a symlink — skipping", link
            )
            return None
        else:
            link.symlink_to(cache_path)
            logger.info("Created attachment symlink: %s → %s", link, cache_path)

        return str(link)
