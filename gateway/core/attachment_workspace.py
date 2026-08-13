"""AttachmentWorkspace: per-watcher symlink management for cached attachments.

Extracted from WatcherLifecycle to keep filesystem preparation separate from
watcher orchestration.  The workspace creates a symlink inside the agent's
working directory that points to the connector's global attachment cache, so
the agent sees attachment files as cwd-local paths (avoiding out-of-project
permission prompts from Claude Code).

Layout::

    {working_directory}/.acg-attachments/{path_key}
        → {global_cache}/{connector_name}/{room_id}/

where path_key is a digest of (connector, room_id) — see gateway/core/paths.py for
why the display name is not used here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .connector import Attachment, Connector
from .paths import resolve_under

logger = logging.getLogger("agent-chat-gateway.core.attachment_workspace")


def localize_attachment_paths(
    attachments: list[Attachment],
    local_base: str | None = None,
) -> list[str]:
    """Remap attachment paths through a per-watcher symlink directory.

    If ``local_base`` is set (e.g. ``{cwd}/.acg-attachments/{path_key}``
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
        path_key: str,
        room_id: str,
        working_directory: str,
    ) -> str | None:
        """Create or update a per-room symlink for cached attachments.

        ``path_key`` is a digest of (connector, room_id) rather than the watcher's
        display name. The name used to be this path component, which made it
        load-bearing: renaming a room orphaned the old link, and a collision pointed one
        room's attachment path at another room's files (§2.3). The cost is that a
        directory listing no longer reads as room names, which `list` offsets by showing
        both.

        Returns:
            Absolute path to the symlink directory (str) if the connector
            supports attachment caching, or ``None`` otherwise.
        """
        cache_dir = self._connector.attachment_cache_dir(room_id)
        if not cache_dir:
            return None

        acg_dir = Path(working_directory) / ".acg-attachments"
        acg_dir.mkdir(parents=True, exist_ok=True)

        # Containment is checked on the link's *location*, never by resolving through
        # it: this link points outside working_directory on purpose, at the global
        # cache, so demanding that its target stay inside would always fail (§2.3).
        link = resolve_under(acg_dir, path_key)
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)

        if not self._ensure_link(link, cache_path):
            return None

        return str(link)

    @staticmethod
    def _ensure_link(link: Path, cache_path: Path) -> bool:
        """Make `link` a symlink to `cache_path`, tolerating a concurrent writer.

        One operation rather than three independent branches, because the room-keyed link
        is *shared* by a room's watchers and the lifecycle locks are per watcher. Every
        step here is a check followed by an act, and another thread can intervene between
        them:

        * both observe it absent, and one `symlink_to` loses;
        * both observe a stale target, and one `unlink` loses (`FileNotFoundError`) or one
          `symlink_to` loses (`FileExistsError`).

        The first version of this fix handled only the absent case — patching the branch
        that was reported and leaving its twin, which is the mistake this file is now
        arranged to prevent. So the whole sequence retries once and then judges by the
        end state: what matters is that the link points at `cache_path` when this returns,
        not which thread put it there.

        Returns False (without raising) only for the one case that is not a race: a real
        file or directory occupying the path, which is an operator problem rather than a
        concurrent writer.
        """
        for _ in range(2):
            if link.is_symlink():
                if link.resolve() == cache_path.resolve():
                    return True
                try:
                    link.unlink()
                except FileNotFoundError:
                    # Another thread removed it first; re-evaluate rather than assume.
                    continue
                logger.info("Repointing attachment symlink: %s → %s", link, cache_path)
            elif link.exists():
                logger.warning(
                    "Attachment path %s exists but is not a symlink — skipping", link
                )
                return False
            try:
                link.symlink_to(cache_path)
                logger.info("Created attachment symlink: %s → %s", link, cache_path)
                return True
            except FileExistsError:
                # Someone created it between the check and here. If it is already what
                # this call wanted, that is success; otherwise loop to re-evaluate.
                if link.is_symlink() and link.resolve() == cache_path.resolve():
                    logger.debug(
                        "Attachment symlink %s created concurrently — reusing it", link
                    )
                    return True

        # Judged by the end state after the bounded retry: a third writer would have to
        # be actively fighting for this path, and reporting the truth beats looping.
        ok = link.is_symlink() and link.resolve() == cache_path.resolve()
        if not ok:
            logger.warning(
                "Could not establish attachment symlink %s → %s after a retry",
                link, cache_path,
            )
        return ok

