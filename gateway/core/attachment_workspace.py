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
import os
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
    """Manages per-room attachment symlinks inside agent working directories.

    Usage::

        from gateway.core.paths import room_path_key

        workspace = AttachmentWorkspace(connector)
        local_base = workspace.setup(
            room_path_key(connector_name, room_id), room_id, working_directory
        )
        # local_base is either a str path or None if attachments are unsupported

    The example passes the key explicitly because passing a watcher name instead would
    *work* — it satisfies `resolve_under` — and silently restore per-watcher links, so the
    misuse produces no error. See gateway/core/paths.py for which key belongs where.
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

        ``path_key`` identifies the ROOM — pass `room_path_key(...)`. The watcher's
        display name used to be this path component, which made it load-bearing: renaming
        a room orphaned the old link, and a collision pointed one room's attachment path
        at another room's files (§2.3). Which key belongs to which artifact is documented
        once, in gateway/core/paths.py; this docstring deliberately does not restate it.
        The cost is that a directory listing no longer reads as room names, which `list`
        offsets by showing both.

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
        """Point `link` at `cache_path`, atomically, tolerating a concurrent writer.

        The room-keyed link is *shared* by a room's watchers while the lifecycle locks are
        per watcher, so another thread can act between any check and the act that follows
        it. Three ways that bites, and the third is why this uses a rename:

        * both observe it absent, and one `symlink_to` loses;
        * both observe a stale target, and one `unlink` loses;
        * one observes a stale target, **pauses**, the other replaces the link and starts
          its watcher — and the paused thread then unlinks a link that is now correct.
          The second watcher can localize an attachment during that gap and fall back to
          the out-of-project cache path, which is what triggers the permission prompts
          this symlink exists to avoid.

        No amount of exception handling fixes the third: nothing raises. The window is
        what has to go, so the replacement is a `symlink` to a temporary name followed by
        `os.replace`, which swaps it in atomically — there is never a moment with no link.
        An earlier version of this method unlinked and re-created, which is exactly that
        window.
        """
        target = str(cache_path)
        for _ in range(2):
            if link.is_symlink():
                if os.readlink(link) == target or link.resolve() == cache_path.resolve():
                    return True
            elif link.exists():
                logger.warning(
                    "Attachment path %s exists but is not a symlink — skipping", link
                )
                return False

            # Unique per attempt and per process so two writers never collide on the
            # temporary name itself.
            tmp = link.with_name(f".{link.name}.{os.getpid()}.{id(link):x}.tmp")
            try:
                tmp.symlink_to(cache_path)
            except FileExistsError:
                tmp.unlink(missing_ok=True)
                tmp.symlink_to(cache_path)
            try:
                # Atomic on POSIX when both paths are in one directory: the link either
                # points at the old target or the new one, never at nothing.
                os.replace(tmp, link)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                # A directory in the way is the one non-race case; report it as such
                # rather than retrying.
                if link.exists() and not link.is_symlink():
                    logger.warning(
                        "Attachment path %s exists but is not a symlink — skipping", link
                    )
                    return False
                logger.debug("Attachment symlink swap for %s failed (%s) — retrying", link, e)
                continue
            logger.info("Attachment symlink ready: %s → %s", link, cache_path)
            return True

        ok = link.is_symlink() and link.resolve() == cache_path.resolve()
        if not ok:
            logger.warning(
                "Could not establish attachment symlink %s → %s after a retry",
                link, cache_path,
            )
        return ok
