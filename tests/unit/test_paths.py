"""Filesystem keys for per-room artifacts, and the containment they need.

Design §2.3 moves two paths off the watcher's display name —
`RUNTIME_DIR/system-prompts/<name>.md` and
`{working_directory}/.acg-attachments/<name>` — onto derived keys, so that a channel
rename, a group DM's membership changing, or an improved sanitizer can no longer point
two rooms at one path.

The two keys differ, and `TestThePromptKeyIsScopedToTheWatcher` below is why: the
attachment workspace keys on the room, while the prompt file keys on the watcher in a
room, because two watchers may bind different agents to one room.

The golden vector below is the load-bearing test in this file. Once these paths key on a
digest, the digest *is* the persistent identity of the files: any change to how it is
derived silently orphans every existing prompt file and symlink — the exact mass
orphaning this change exists to prevent. Nothing else would fail, because the prompt
file is rewritten and the symlink re-created on every watcher start, so a refactor could
change the derivation and leave a growing pile of dead files with a green suite.

Run with:
    uv run python -m pytest tests/unit/test_paths.py -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gateway.core.paths import (
    remove_workspace_link,
    resolve_under,
    room_path_key,
    watcher_prompt_key,
)


class TestTheKeyIsStable(unittest.TestCase):
    def test_golden_vectors(self):
        """Pinned outputs. If these change, every deployed installation's prompt files
        and attachment symlinks are orphaned in one release — so this test failing is a
        migration decision, never a formatting fix.

        Taken from the implementation, which is worth being explicit about: a vector
        captured this way guards *change*, not correctness — there is no external
        specification for this digest to be correct against. Detecting change is
        precisely the job here, since nothing else in the suite can see a derivation
        that silently starts producing different paths.
        """
        self.assertEqual(
            room_path_key("rc-home", "GENERAL123"),
            "b97adca1aacb510939e9625140991156",
        )
        self.assertEqual(
            room_path_key("mm-lab", "c9x8y7z6"),
            "d95eddf8e335ae07cb6fc2f00fd5fbc1",
        )

    def test_the_same_pair_always_gives_the_same_key(self):
        """Not a tautology: Python's builtin `hash()` is salted per process, so a key
        built from it would differ across restarts — and nothing would look broken,
        because both artifacts are recreated on every watcher start. It would just leak
        one dead file and one dead symlink per boot, silently. This asserts the
        derivation is a real digest instead."""
        first = room_path_key("rc", "room1")
        second = room_path_key("rc", "room1")
        self.assertEqual(first, second)

    def test_the_key_survives_a_subprocess(self):
        """The actual property a per-process salt would break, checked across a real
        process boundary rather than inferred."""
        import subprocess
        import sys

        code = (
            "from gateway.core.paths import room_path_key;"
            "print(room_path_key('rc', 'room1'))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONHASHSEED": "random"},
        ).stdout.strip()
        self.assertEqual(out, room_path_key("rc", "room1"))

    def test_different_rooms_and_connectors_differ(self):
        keys = {
            room_path_key("rc", "a"),
            room_path_key("rc", "b"),
            room_path_key("mm", "a"),
            room_path_key("mm", "b"),
        }
        self.assertEqual(len(keys), 4)

    def test_the_encoding_is_unambiguous(self):
        """Concatenating the two parts would make ("ab","c") and ("a","bc") the same
        bytes — and therefore give two different rooms one path."""
        self.assertNotEqual(room_path_key("ab", "c"), room_path_key("a", "bc"))
        self.assertNotEqual(room_path_key("a-b", "c"), room_path_key("a", "b-c"))

    def test_a_hostile_room_id_yields_one_safe_component(self):
        """`room_id` is external connector data; §2.3's whole reason for deriving rather
        than using it raw. Each of these would escape, collide, or overflow a path if
        used directly."""
        for room_id in (
            "../../etc/passwd", "/absolute", "-leading-dash", "with space",
            "a" * 10_000, "unicode-日本語", "nul\x00byte", "", ".", "..",
            "trailing/slash/", "back\\slash",
        ):
            with self.subTest(room_id=room_id[:24]):
                key = room_path_key("rc", room_id)
                self.assertEqual(len(key), 32)
                self.assertTrue(key.isalnum(), key)
                self.assertEqual(Path(key).name, key, "not a single path component")


class TestThePromptKeyIsScopedToTheWatcher(unittest.TestCase):
    """Two watchers can share a room, and their durable instructions differ.

    A room-only key made the later watcher overwrite the first one's identity and
    context, after which both processors used the overwritten file on every turn —
    silently, and only in a configuration the static shape still permits: two watchers
    binding different agents to one connector+room, which loads today and which
    `MessageDispatcher` fans messages out to. The content is built from the agent and the
    watcher's own context files, so it is not room-determined while that is expressible.

    This is a deliberate deviation from §2.3, which lists both artifacts under one
    room-scoped key — correct once a room has exactly one watcher, which is the manager's
    model rather than today's.
    """

    def test_two_watchers_in_one_room_get_different_prompt_keys(self):
        a = watcher_prompt_key("rc", "ROOM1", "w-agent-a")
        b = watcher_prompt_key("rc", "ROOM1", "w-agent-b")
        self.assertNotEqual(a, b)

    def test_the_same_watcher_always_gets_the_same_prompt_key(self):
        self.assertEqual(
            watcher_prompt_key("rc", "ROOM1", "w"),
            watcher_prompt_key("rc", "ROOM1", "w"),
        )

    def test_the_same_name_in_two_rooms_cannot_collide(self):
        """The residual risk of putting a name back in the key would be a collision; the
        room is in the digest, so there isn't one."""
        self.assertNotEqual(
            watcher_prompt_key("rc", "ROOM1", "w"),
            watcher_prompt_key("rc", "ROOM2", "w"),
        )

    def test_it_differs_from_the_room_key(self):
        """The attachment workspace keys on the room — shared by definition, since the
        cache it links to is per room. The two keys must not be interchangeable."""
        self.assertNotEqual(
            watcher_prompt_key("rc", "ROOM1", "w"),
            room_path_key("rc", "ROOM1"),
        )

    def test_the_encoding_stays_unambiguous_with_three_parts(self):
        self.assertNotEqual(
            watcher_prompt_key("rc", "a", "bc"),
            watcher_prompt_key("rc", "ab", "c"),
        )

    def test_golden_vector(self):
        """Pinned for the same reason as the room key: this digest is the identity of a
        file on disk, so a change to the derivation orphans every existing one."""
        self.assertEqual(
            watcher_prompt_key("rc-home", "GENERAL123", "rc-home-general"),
            "018ecbde0cb23711a071b2816270888a",
        )

    def test_a_hostile_watcher_name_yields_one_safe_component(self):
        for name in ("../escape", "/abs", "a" * 5000, "nul\x00", ""):
            with self.subTest(name=name[:16]):
                key = watcher_prompt_key("rc", "r", name)
                self.assertEqual(len(key), 32)
                self.assertTrue(key.isalnum())


class TestContainment(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()

    def test_a_normal_join_is_returned_in_the_caller_s_own_form(self):
        """Verified against the resolved path, returned unresolved.

        Returning the resolved form canonicalises it — on macOS a path under `/var`
        comes back under `/private/var` — which changes what callers log and compare
        for no benefit, since the check has already established where it lands. Three
        adapter tests caught exactly that when the first version returned the resolved
        path.
        """
        got = resolve_under(self.root, "abc123.md")
        self.assertEqual(got, self.root / "abc123.md")
        self.assertEqual(got.parent, self.root)

    def test_traversal_is_refused(self):
        for parts in ((".."), ("..", "escaped"), ("../../etc/passwd",)):
            with self.subTest(parts=parts):
                with self.assertRaises(ValueError):
                    resolve_under(self.root, *([parts] if isinstance(parts, str) else parts))

    def test_an_absolute_part_is_refused(self):
        """`Path.joinpath` lets an absolute component *replace* the root, so this is not
        hypothetical — it is the default behaviour being guarded against."""
        with self.assertRaises(ValueError):
            resolve_under(self.root, "/etc/passwd")

    def test_a_symlinked_leaf_is_judged_by_location_not_target(self):
        """The attachment link points outside the working directory on purpose, at the
        global cache. Resolving *through* it and demanding containment would refuse
        every legitimate workspace, so the check looks at where the link lives."""
        outside = Path(self._tmp.name) / "global-cache"
        outside.mkdir()
        link = self.root / "roomkey"
        link.symlink_to(outside)
        got = resolve_under(self.root, "roomkey")
        self.assertEqual(got, self.root / "roomkey")

    def test_a_symlinked_parent_that_escapes_is_refused(self):
        """The complement: if a *directory* on the way out has been replaced by a link
        pointing elsewhere, the constructed path really is outside the root."""
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        (self.root / "sub").symlink_to(outside)
        with self.assertRaises(ValueError):
            resolve_under(self.root, "sub", "file.md")


class TestSymlinkSafeDeletion(unittest.TestCase):
    """"Define symlink handling before deletion" (§2.3).

    Only `impl/expiry` consumes this, but the definition belongs beside the paths it
    protects: the attachment workspace is *made of* symlinks pointing at the global
    cache, so a reclamation that recursed through them would delete the cache itself.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_symlink_is_unlinked_and_its_target_survives(self):
        victim = self.tmp / "global-cache"
        victim.mkdir()
        (victim / "attachment.png").write_text("payload")
        link = self.tmp / "workspace-link"
        link.symlink_to(victim)

        remove_workspace_link(link)

        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertTrue(victim.is_dir(), "deletion followed the link out of the tree")
        self.assertEqual((victim / "attachment.png").read_text(), "payload")

    def test_a_dangling_symlink_is_still_removed(self):
        """`exists()` is False for a dangling link, so an exists-first check would leave
        it behind forever."""
        link = self.tmp / "dangling"
        link.symlink_to(self.tmp / "never-existed")
        self.assertFalse(link.exists())
        remove_workspace_link(link)
        self.assertFalse(link.is_symlink())

    def test_a_directory_of_links_is_removed_without_following_them(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")
        workspace = self.tmp / ".acg-attachments"
        workspace.mkdir()
        (workspace / "roomkey").symlink_to(victim)

        remove_workspace_link(workspace)

        self.assertFalse(workspace.exists())
        self.assertTrue(victim.is_dir(), "rmtree followed a link out of the tree")
        self.assertEqual((victim / "keep.txt").read_text(), "keep")

    def test_a_missing_path_is_not_an_error(self):
        remove_workspace_link(self.tmp / "never-there")

    def test_a_plain_file_is_removed(self):
        f = self.tmp / "prompt.md"
        f.write_text("x")
        remove_workspace_link(f)
        self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()
