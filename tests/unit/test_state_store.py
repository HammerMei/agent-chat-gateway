"""Tests for gateway/core/state_store.py — StateStore's merge-on-save contract.

StateStore had no direct tests. It needs them, because it is the layer that
decides *which* records survive a write: `save_state` underneath it rewrites the
whole file, so a StateStore that passed through only the caller's dict silently
erased every record the caller did not happen to hold — and callers routinely
hold a subset.

Deliberately does NOT use tests/helpers.py's IsolatedTestCase: that patches
`load_state`/`save_state` at module scope, which would stub out exactly the
behaviour under test.  These exercise the real functions against a temp
RUNTIME_DIR.

Run with:
    uv run python -m pytest tests/unit/test_state_store.py -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.core.state import WatcherState, load_state, save_state
from gateway.core.state_store import StateStore


def _state(name: str, session: str = "", room_id: str = "", **kw) -> WatcherState:
    return WatcherState(
        watcher_name=name,
        session_id=session or f"sess-{name}",
        room_id=room_id or f"room-{name}",
        **kw,
    )


class StateStoreTestCase(unittest.TestCase):
    """Real save_state/load_state against a temp RUNTIME_DIR."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._runtime = patch("gateway.core.state.RUNTIME_DIR", Path(self.tmp))
        self._runtime.start()
        # A connector that reports no live watermark, so persistence assertions
        # are not perturbed by the watermark pull.  Tests that care override it.
        self.connector = MagicMock()
        self.connector.get_last_processed_ts = MagicMock(return_value=None)
        self.store = StateStore("rc-test", self.connector)

    def tearDown(self):
        self._runtime.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_disk(self) -> dict[str, WatcherState]:
        return {ws.watcher_name: ws for ws in load_state("rc-test")}


class TestSaveMerges(StateStoreTestCase):
    """A record the caller does not hold must survive the write."""

    def test_a_record_absent_from_the_saved_dict_survives(self):
        """The core regression. save() is routinely called with a subset."""
        save_state("rc-test", [_state("kept"), _state("held")])

        self.store.save({"held": _state("held", session="sess-new")})

        disk = self._on_disk()
        self.assertIn("kept", disk, "record absent from the saved dict was erased")
        self.assertEqual(disk["kept"].session_id, "sess-kept")
        self.assertEqual(disk["held"].session_id, "sess-new", "held record must win")

    def test_in_memory_record_wins_over_disk(self):
        save_state("rc-test", [_state("w", session="sess-old")])

        self.store.save({"w": _state("w", session="sess-new")})

        self.assertEqual(self._on_disk()["w"].session_id, "sess-new")

    def test_paused_flag_of_an_unheld_record_is_preserved(self):
        """A pause is an explicit operator instruction; losing it un-mutes a room."""
        save_state("rc-test", [_state("muted", paused=True), _state("other")])

        self.store.save({"other": _state("other")})

        self.assertTrue(self._on_disk()["muted"].paused)

    def test_watermark_of_an_unheld_record_is_preserved(self):
        """Losing a watermark re-delivers messages after a restart."""
        save_state("rc-test", [_state("quiet", last_processed_ts="2025-01-01T00:00:09Z")])

        self.store.save({})

        self.assertEqual(
            self._on_disk()["quiet"].last_processed_ts, "2025-01-01T00:00:09Z"
        )

    def test_saving_an_empty_dict_preserves_everything_on_disk(self):
        save_state("rc-test", [_state("a"), _state("b")])

        self.store.save({})

        self.assertEqual(set(self._on_disk()), {"a", "b"})

    def test_a_new_record_is_added_without_disturbing_existing_ones(self):
        save_state("rc-test", [_state("old")])

        self.store.save({"new": _state("new")})

        self.assertEqual(set(self._on_disk()), {"old", "new"})

    def test_missing_state_file_still_writes_the_held_records(self):
        self.store.save({"first": _state("first")})

        self.assertEqual(set(self._on_disk()), {"first"})

    def test_corrupt_state_file_degrades_to_replace_rather_than_raising(self):
        """load_state swallows corruption and returns []; save must not explode."""
        (Path(self.tmp) / "state.rc-test.json").write_text("{ not json")

        self.store.save({"w": _state("w")})

        self.assertEqual(set(self._on_disk()), {"w"})


class TestPruneIsExplicit(StateStoreTestCase):
    """Deliberate removal has to be named, since merging would otherwise keep it."""

    def test_pruned_record_is_dropped(self):
        save_state("rc-test", [_state("gone"), _state("stays")])

        self.store.save({"stays": _state("stays")}, prune={"gone"})

        self.assertEqual(set(self._on_disk()), {"stays"})

    def test_pruning_a_name_that_is_not_persisted_is_a_no_op(self):
        save_state("rc-test", [_state("a")])

        self.store.save({"a": _state("a")}, prune={"never-existed"})

        self.assertEqual(set(self._on_disk()), {"a"})

    def test_prune_wins_over_a_held_record_of_the_same_name(self):
        """Prune is the caller's explicit intent, so it takes precedence."""
        save_state("rc-test", [_state("doomed")])

        self.store.save({"doomed": _state("doomed")}, prune={"doomed"})

        self.assertEqual(self._on_disk(), {})

    def test_no_prune_argument_keeps_everything(self):
        save_state("rc-test", [_state("a"), _state("b")])

        self.store.save({"a": _state("a")})

        self.assertEqual(set(self._on_disk()), {"a", "b"})


class TestWatermarkPull(StateStoreTestCase):
    """The pull is best-effort and only applies to records the caller holds."""

    def test_live_watermark_overwrites_the_held_value(self):
        self.connector.get_last_processed_ts = MagicMock(return_value="2025-06-06T06:06:06Z")

        self.store.save({"w": _state("w", last_processed_ts="stale")})

        self.assertEqual(self._on_disk()["w"].last_processed_ts, "2025-06-06T06:06:06Z")

    def test_no_live_watermark_leaves_the_held_value(self):
        self.connector.get_last_processed_ts = MagicMock(return_value=None)

        self.store.save({"w": _state("w", last_processed_ts="known")})

        self.assertEqual(self._on_disk()["w"].last_processed_ts, "known")

    def test_a_raising_connector_does_not_abort_the_save(self):
        self.connector.get_last_processed_ts = MagicMock(side_effect=RuntimeError("torn down"))

        self.store.save({"w": _state("w", last_processed_ts="known")})

        self.assertEqual(self._on_disk()["w"].last_processed_ts, "known")

    def test_records_read_back_from_disk_are_not_polled(self):
        """They have no connector-side room state, so polling them is wasted."""
        save_state("rc-test", [_state("on-disk-only")])
        self.connector.get_last_processed_ts = MagicMock(return_value="live")

        self.store.save({"held": _state("held")})

        polled = {c.args[0] for c in self.connector.get_last_processed_ts.call_args_list}
        self.assertNotIn("room-on-disk-only", polled)
        self.assertEqual(self._on_disk()["on-disk-only"].last_processed_ts, "")

    def test_a_record_without_a_room_id_is_not_polled(self):
        self.store.save({"w": WatcherState(watcher_name="w", session_id="s", room_id="")})

        self.connector.get_last_processed_ts.assert_not_called()


class TestLoad(StateStoreTestCase):
    def test_load_keys_by_watcher_name(self):
        save_state("rc-test", [_state("a"), _state("b")])

        self.assertEqual(set(self.store.load()), {"a", "b"})

    def test_load_of_a_missing_file_is_empty(self):
        self.assertEqual(self.store.load(), {})


if __name__ == "__main__":
    unittest.main()
