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

    def test_a_concurrently_created_link_pointing_elsewhere_is_repointed(self):
        """Repointed, not refused — the same thing that happens without a race.

        An earlier version of this fix raised here, on the reasoning that a link aimed
        elsewhere must not be swallowed. That was inconsistent: the non-concurrent path
        already repoints a stale link (see the test below), so the outcome would have
        depended on *when* the wrong target was noticed.

        And repointing is not swallowing. `attachment_cache_dir` depends only on
        (connector, room id), so two watchers in one room cannot genuinely disagree about
        the target; a wrong one means the link is stale — after `cache_dir_global`
        changed, say — which is precisely what repointing is for. The watcher ends up
        reading its own room's cache either way, which was the actual concern.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        stale = self.tmp / "old-cache-location"
        stale.mkdir()
        (acg / "ROOMKEY").symlink_to(stale)

        with patch.object(Path, "is_symlink", _blind_once("ROOMKEY", Path.is_symlink)), \
             patch.object(Path, "exists", _blind_once("ROOMKEY", Path.exists)):
            got = self.workspace.setup("ROOMKEY", "room1", str(self.work))

        self.assertEqual(got, str(acg / "ROOMKEY"))
        self.assertEqual((acg / "ROOMKEY").resolve(), self.cache.resolve())

    def test_a_stale_link_removed_underneath_is_not_an_error(self):
        """The twin of the absent-link race, in the *update* branch.

        Two same-room watchers resumed while the link is stale can both enter the
        wrong-target branch; one `unlink` then loses with `FileNotFoundError`. The first
        version of this fix covered only the create branch — patching the reported case
        and leaving its sibling, which is the shape this file now guards against.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        stale = self.tmp / "old-cache-location"
        stale.mkdir()
        link = acg / "ROOMKEY"
        link.symlink_to(stale)

        real_unlink = Path.unlink

        def unlink_then_vanish(self, *a, **kw):  # noqa: ANN001 - patched method
            if self.name == "ROOMKEY":
                real_unlink(self, *a, **kw)
                # Simulate the losing thread: the link is already gone when it tries.
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_unlink(self, *a, **kw)

        with patch.object(Path, "unlink", unlink_then_vanish):
            got = self.workspace.setup("ROOMKEY", "room1", str(self.work))

        self.assertEqual(got, str(link))
        self.assertEqual(link.resolve(), self.cache.resolve())

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

    def test_the_refusal_says_what_is_actually_wrong(self):
        """Asserted on the message, because the *outcome* alone cannot distinguish this
        from a lost race.

        Removing the explicit not-a-symlink branch still returns None — the bounded retry
        exhausts and the end-state check refuses — so a test on the return value alone
        passes either way. What degrades is diagnosability: an operator told "could not
        establish after a retry" looks for a concurrent writer, when the real cause is a
        stray directory they can simply remove. That difference is the whole value of the
        branch, so it is what gets asserted.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        (acg / "ROOMKEY").mkdir()
        with self.assertLogs(
            "agent-chat-gateway.core.attachment_workspace", level="WARNING"
        ) as logs:
            self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertTrue(
            any("not a symlink" in line for line in logs.output),
            f"the refusal did not name the cause: {logs.output}",
        )

    def test_a_connector_without_attachment_support_gets_no_workspace(self):
        workspace = AttachmentWorkspace(_FakeConnector(None))
        self.assertIsNone(workspace.setup("ROOMKEY", "room1", str(self.work)))
        self.assertFalse((self.work / ".acg-attachments").exists())

    def test_the_link_is_never_absent_while_being_repointed(self):
        """The window, not the exception.

        A thread that observed a stale target, paused, and resumed after another thread
        had already repointed the link would `unlink` a *correct* link and re-create it.
        Nothing raises; there is simply a moment with no link, and the other watcher can
        localize an attachment in it — falling back to the out-of-project cache path,
        which is what triggers the permission prompts this symlink exists to avoid.

        Asserted as a property rather than by timing: `unlink` must not be reached on this
        path at all. `os.replace` swaps the link atomically instead, so the window cannot
        exist regardless of interleaving.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        stale = self.tmp / "old-cache"
        stale.mkdir()
        (acg / "ROOMKEY").symlink_to(stale)

        real_unlink = Path.unlink
        unlinked: list[str] = []

        def recording_unlink(self, *a, **kw):  # noqa: ANN001 - patched method
            unlinked.append(self.name)
            return real_unlink(self, *a, **kw)

        with patch.object(Path, "unlink", recording_unlink):
            got = self.workspace.setup("ROOMKEY", "room1", str(self.work))

        self.assertEqual(got, str(acg / "ROOMKEY"))
        self.assertEqual((acg / "ROOMKEY").resolve(), self.cache.resolve())
        self.assertNotIn(
            "ROOMKEY", unlinked,
            "the live link was unlinked before being re-created, which leaves a window "
            f"where it does not exist (unlinked: {unlinked})",
        )

    def test_the_swap_leaves_no_temporary_behind(self):
        """A rename-based swap that failed to clean up would litter the user's project
        directory, which is what `.acg-attachments` lives in."""
        self.workspace.setup("ROOMKEY", "room1", str(self.work))
        acg = self.work / ".acg-attachments"
        leftovers = [p.name for p in acg.iterdir() if p.name != "ROOMKEY"]
        self.assertEqual(leftovers, [], f"temporary files left behind: {leftovers}")

    def test_a_hostile_key_cannot_escape_the_workspace(self):
        """`resolve_under` is what stands between an unexpected key and the filesystem."""
        for key in ("../escape", "/abs", "..", "a/b"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.workspace.setup(key, "room1", str(self.work))


if __name__ == "__main__":
    unittest.main()
