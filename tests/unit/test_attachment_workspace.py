"""The attachment workspace, now that one link is shared by a room's watchers.

Keying the link on `(connector, room_id)` rather than on the watcher name means two
watchers in one room use the *same* link. `setup()` checked for the link and then created
it with no synchronisation between those two steps, and the lifecycle locks are per
watcher, so two concurrent starts could both find it absent and the loser would raise
`FileExistsError` — rolling that watcher's whole startup back. Before the re-key the two
watchers had separate links and could not race at all, so this was introduced by that
change.

The race is tested deterministically rather than by running two starts and hoping to hit
the window: the interleaving is simulated by making the existence checks report absent
for a link that is already there, which is exactly what the losing thread observes.

Run with:
    uv run python -m pytest tests/unit/test_attachment_workspace.py -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.core.attachment_workspace import AttachmentWorkspace


class _FakeConnector:
    def __init__(self, cache_dir: str | None) -> None:
        self._cache_dir = cache_dir

    def attachment_cache_dir(self, room_id: str) -> str | None:
        return self._cache_dir


def _blind_once(name: str, real):
    """Report a path as absent for its FIRST check only.

    That is precisely the losing thread's view: it checks before the winner has created
    the link, and re-checks *after* `symlink_to` fails — by which point the link is
    there. A blanket patch blinds the recovery check too, which is not what happens, and
    which made the first version of this test fail against correct code: the test was
    wrong, not the fix.

    A closure rather than a callable class, because the patched attribute is invoked as
    an unbound method (`Path.is_symlink(p)`), and a class instance would receive `self`
    in that position.
    """
    seen = {"n": 0}

    def patched(path):
        if path.name == name:
            seen["n"] += 1
            if seen["n"] == 1:
                return False
        return real(path)

    return patched


class TestAttachmentWorkspaceSetup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "global-cache" / "room1"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.workspace = AttachmentWorkspace(_FakeConnector(str(self.cache)))

    def test_creates_the_link_named_by_the_key(self):
        got = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        link = self.work / ".acg-attachments" / "ROOMKEY"
        self.assertEqual(got, str(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.cache.resolve())

    def test_is_idempotent(self):
        first = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        second = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertEqual(first, second)

    def test_a_link_created_concurrently_is_reused_not_an_error(self):
        """The losing thread's exact view: checks say absent, creation says it exists.

        Simulated rather than raced, so the test cannot pass by missing the window. What
        it pins is that `setup()` treats an already-correct link as success — otherwise
        one of two same-room watchers fails to start, and its rollback undoes state the
        other one is relying on.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        self.cache.mkdir(parents=True)
        (acg / "ROOMKEY").symlink_to(self.cache)

        with patch.object(Path, "is_symlink", _blind_once("ROOMKEY", Path.is_symlink)), \
             patch.object(Path, "exists", _blind_once("ROOMKEY", Path.exists)):
            got = self.workspace.setup("ROOMKEY", "room1", str(self.work))

        self.assertEqual(got, str(acg / "ROOMKEY"))

    def test_a_concurrently_created_link_pointing_elsewhere_still_raises(self):
        """The tolerance is narrow on purpose: only a link that already points where this
        call wanted it counts as success. Anything else is a real conflict and must not be
        swallowed, or a watcher would silently read another room's attachments."""
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        wrong = self.tmp / "someone-elses-cache"
        wrong.mkdir()
        (acg / "ROOMKEY").symlink_to(wrong)

        with patch.object(Path, "is_symlink", _blind_once("ROOMKEY", Path.is_symlink)), \
             patch.object(Path, "exists", _blind_once("ROOMKEY", Path.exists)):
            with self.assertRaises(FileExistsError):
                self.workspace.setup("ROOMKEY", "room1", str(self.work))

    def test_an_existing_link_to_the_wrong_target_is_repointed(self):
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        stale = self.tmp / "stale-cache"
        stale.mkdir()
        (acg / "ROOMKEY").symlink_to(stale)

        self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertEqual((acg / "ROOMKEY").resolve(), self.cache.resolve())

    def test_a_real_directory_in_the_way_is_refused_rather_than_replaced(self):
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        (acg / "ROOMKEY").mkdir()
        self.assertIsNone(self.workspace.setup("ROOMKEY", "room1", str(self.work)))

    def test_a_connector_without_attachment_support_gets_no_workspace(self):
        workspace = AttachmentWorkspace(_FakeConnector(None))
        self.assertIsNone(workspace.setup("ROOMKEY", "room1", str(self.work)))
        self.assertFalse((self.work / ".acg-attachments").exists())

    def test_a_hostile_key_cannot_escape_the_workspace(self):
        """`resolve_under` is what stands between an unexpected key and the filesystem."""
        for key in ("../escape", "/abs", "..", "a/b"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.workspace.setup(key, "room1", str(self.work))


if __name__ == "__main__":
    unittest.main()
