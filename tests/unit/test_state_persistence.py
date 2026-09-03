"""Tests for gateway/core/state.py — save_state() / load_state() and WatcherState.

Covers the previously-untested save/load round-trip, legacy format migration,
atomic write pattern, and corrupt-file recovery.

Run with:
    uv run python -m pytest tests/test_state_persistence.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_state(name="watcher-1", session="sess-abc", room_id="room-1"):
    from gateway.core.state import WatcherState
    return WatcherState(
        watcher_name=name,
        session_id=session,
        room_id=room_id,
        room_type="channel",
        context_injected=True,
        paused=False,
        last_processed_ts="2025-01-01T00:00:01.000Z",
    )


class TestStatePersistence(unittest.TestCase):
    """save_state() / load_state() round-trip and edge cases."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._runtime_patch = patch(
            "gateway.core.state.RUNTIME_DIR", Path(self.tmp)
        )
        self._runtime_patch.start()

    def tearDown(self):
        self._runtime_patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── Round-trip ────────────────────────────────────────────────────────────

    def test_save_and_load_round_trip_single(self):
        """A saved WatcherState survives a load_state() call unchanged."""
        from gateway.core.state import load_state, save_state

        ws = _make_state()
        save_state("rc", [ws])
        loaded = load_state("rc")

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].watcher_name, "watcher-1")
        self.assertEqual(loaded[0].session_id, "sess-abc")
        self.assertEqual(loaded[0].room_id, "room-1")
        self.assertEqual(loaded[0].room_type, "channel")
        self.assertTrue(loaded[0].context_injected)
        self.assertFalse(loaded[0].paused)
        self.assertEqual(loaded[0].last_processed_ts, "2025-01-01T00:00:01.000Z")

    def test_save_and_load_multiple_watchers(self):
        """Multiple watchers saved and loaded preserve all entries."""
        from gateway.core.state import load_state, save_state

        states = [
            _make_state("w1", "s1", "r1"),
            _make_state("w2", "s2", "r2"),
            _make_state("w3", "s3", "r3"),
        ]
        save_state("rc", states)
        loaded = load_state("rc")

        self.assertEqual(len(loaded), 3)
        names = {w.watcher_name for w in loaded}
        self.assertEqual(names, {"w1", "w2", "w3"})

    def test_save_overwrites_previous(self):
        """A second save_state() replaces the previous file contents."""
        from gateway.core.state import load_state, save_state

        save_state("rc", [_make_state("original")])
        save_state("rc", [_make_state("updated")])
        loaded = load_state("rc")

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].watcher_name, "updated")

    def test_connector_namespacing(self):
        """Different connector names use separate state files."""
        from gateway.core.state import load_state, save_state

        save_state("rc-prod", [_make_state("prod-watcher")])
        save_state("rc-staging", [_make_state("staging-watcher")])

        prod = load_state("rc-prod")
        staging = load_state("rc-staging")

        self.assertEqual(prod[0].watcher_name, "prod-watcher")
        self.assertEqual(staging[0].watcher_name, "staging-watcher")

    # ── No file ───────────────────────────────────────────────────────────────

    def test_load_returns_empty_when_no_file(self):
        """load_state() returns [] when no state file exists yet."""
        from gateway.core.state import load_state

        result = load_state("nonexistent-connector")
        self.assertEqual(result, [])

    def test_save_empty_list_clears_state(self):
        """Saving an empty list produces a valid file that loads back as []."""
        from gateway.core.state import load_state, save_state

        save_state("rc", [_make_state()])
        save_state("rc", [])
        loaded = load_state("rc")
        self.assertEqual(loaded, [])

    # ── Legacy format refusal (was: migration) ────────────────────────────────

    def test_a_legacy_watcher_id_record_is_refused_not_migrated(self):
        """Inverted with the removal of the legacy branch (design §5.3).

        These two tests asserted that a `watcher_id`-era record was best-effort
        migrated by using `room_name` as the watcher name. That reader is deleted
        rather than extended, because it cannot supply any of the fields on-the-fly
        watchers need — no agent, no materialized config, no originating rule — so
        extending it would manufacture incomplete records and bypass the refusal that
        is the actual contract.

        Kept as tests rather than deleted: what mattered about them is that a legacy
        file is *noticed*, and that is still the property under test — only the answer
        changed from "migrate it" to "refuse to start".
        """
        from gateway.core.state import LegacyStateError, _state_file, load_state

        state_file = _state_file("rc")
        state_file.write_text(json.dumps({
            "watchers": [
                {
                    "watcher_id": "wid-001",
                    "room_name": "support",
                    "session_id": "sess-legacy",
                    "room_id": "room-legacy",
                    "room_type": "channel",
                    "context_injected": True,
                    "last_processed_ts": "",
                }
            ]
        }))

        with self.assertRaises(LegacyStateError) as cm:
            load_state("rc")
        # The operator has to be able to find the file and the procedure.
        self.assertIn(str(state_file), str(cm.exception))

    def test_a_previous_current_format_record_is_refused_too(self):
        """The refusal is a file-level version check, not record-shape sniffing — so
        the format immediately before this one is refused as well, even though every
        one of its records looks perfectly well-formed."""
        from gateway.core.state import LegacyStateError, _state_file, load_state

        _state_file("rc").write_text(json.dumps({
            "watchers": [
                {"watcher_name": "support", "session_id": "s", "room_id": "r",
                 "room_type": "channel", "context_injected": True, "paused": False,
                 "last_processed_ts": ""}
            ]
        }))
        with self.assertRaises(LegacyStateError):
            load_state("rc")

    # ── Error resilience ──────────────────────────────────────────────────────

    def test_load_corrupt_json_returns_empty_list(self):
        """Corrupt state file → load_state() returns [] and logs a warning."""
        from gateway.core.state import _state_file, load_state

        state_file = _state_file("rc")
        state_file.write_text("{this is not valid json}")

        result = load_state("rc")
        self.assertEqual(result, [])

    def test_load_missing_watcher_name_field_skipped(self):
        """A record with no watcher_name is skipped, in an otherwise current file.

        The file needs its version marker now, or the refusal fires first and this
        stops testing what it says it does — the record-level skip, not the
        file-level check.
        """
        from gateway.core.state import STATE_FORMAT_VERSION, _state_file, load_state

        state_file = _state_file("rc")
        data = {
            "version": STATE_FORMAT_VERSION,
            "watchers": [{"session_id": "s1", "room_id": "r1"}],
        }
        state_file.write_text(json.dumps(data))

        loaded = load_state("rc")
        self.assertEqual(loaded, [])

    # ── Atomic write pattern ──────────────────────────────────────────────────

    def test_save_produces_valid_json(self):
        """The written state file is valid JSON with expected structure."""
        from gateway.core.state import _state_file, save_state

        ws = _make_state()
        save_state("rc", [ws])

        raw = json.loads(_state_file("rc").read_text())
        self.assertIn("watchers", raw)
        self.assertEqual(len(raw["watchers"]), 1)
        self.assertEqual(raw["watchers"][0]["watcher_name"], "watcher-1")

    def test_save_cleans_up_tmp_on_write_error(self):
        """If the write fails, the .tmp file is removed (no leftover)."""
        from gateway.core.state import _state_file, save_state

        # Find where the tmp file would be created
        state_file = _state_file("rc")
        tmp_pattern = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")

        # Patch write_text to fail
        original_write = Path.write_text

        def bad_write(self, *args, **kwargs):
            if str(self).endswith(".tmp"):
                raise OSError("disk full")
            return original_write(self, *args, **kwargs)

        with patch.object(Path, "write_text", bad_write):
            with self.assertRaises(OSError):
                save_state("rc", [_make_state()])

        # Tmp file must be cleaned up
        self.assertFalse(tmp_pattern.exists())

    def test_save_no_leftover_tmp_on_success(self):
        """After a successful save, no .tmp file should remain."""
        from gateway.core.state import _state_file, save_state

        save_state("rc", [_make_state()])
        state_file = _state_file("rc")
        tmp_files = list(state_file.parent.glob(f"{state_file.name}.*.tmp"))
        self.assertEqual(tmp_files, [], f"Unexpected tmp files: {tmp_files}")

    # ── WatcherState fields ───────────────────────────────────────────────────

    def test_paused_flag_persisted(self):
        """paused=True survives round-trip."""
        from gateway.core.state import WatcherState, load_state, save_state

        ws = WatcherState(
            watcher_name="paused-watcher",
            session_id="",
            room_id="r1",
            paused=True,
        )
        save_state("rc", [ws])
        loaded = load_state("rc")
        self.assertTrue(loaded[0].paused)

    def test_last_processed_ts_persisted(self):
        """last_processed_ts survives round-trip."""
        from gateway.core.state import WatcherState, load_state, save_state

        ws = WatcherState(
            watcher_name="ts-watcher",
            session_id="",
            room_id="r1",
            last_processed_ts="2025-06-01T12:00:00.000Z",
        )
        save_state("rc", [ws])
        loaded = load_state("rc")
        self.assertEqual(loaded[0].last_processed_ts, "2025-06-01T12:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
