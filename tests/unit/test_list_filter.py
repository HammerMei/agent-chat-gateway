"""`list`'s state filter, its state derivation, and the merged record view.

Design §2.8: `list` enumerates persisted records, not config entries and not
live processors — so the three pieces that decide *which* records it returns
are tested here, and the rows themselves in
``tests/integration/test_list_watchers.py``.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from gateway.core.state import (
    STATE_FILTER_NAMES,
    StateFilter,
    WatcherState,
    lifecycle_state,
    parse_state_filter,
    state_filter_name,
)
from gateway.core.state_store import StateStore


def _record(name="w1", **kwargs) -> WatcherState:
    return WatcherState(
        watcher_name=name,
        session_id=kwargs.pop("session_id", "s-1"),
        room_id=kwargs.pop("room_id", "room-1"),
        **kwargs,
    )


class TestParseStateFilter(unittest.TestCase):
    def test_absent_filter_is_operable_not_all(self):
        """The default is deliberately narrower than ALL (design §2.8)."""
        self.assertEqual(parse_state_filter(None), StateFilter.OPERABLE)
        self.assertNotEqual(parse_state_filter(None), StateFilter.ALL)

    def test_names_compose(self):
        self.assertEqual(
            parse_state_filter(["active", "idle"]),
            StateFilter.ACTIVE | StateFilter.IDLE,
        )

    def test_all_three_names_equal_all(self):
        self.assertEqual(
            parse_state_filter(["active", "idle", "paused"]), StateFilter.ALL
        )

    def test_repeated_name_is_idempotent(self):
        self.assertEqual(parse_state_filter(["idle", "idle"]), StateFilter.IDLE)

    def test_empty_list_is_an_error_not_the_default(self):
        """An explicit empty selection is a caller bug, not "give me the default"."""
        with self.assertRaises(ValueError):
            parse_state_filter([])

    def test_unknown_name_raises_and_names_the_valid_ones(self):
        with self.assertRaises(ValueError) as ctx:
            parse_state_filter(["active", "sleeping"])
        message = str(ctx.exception)
        self.assertIn("sleeping", message)
        for known in STATE_FILTER_NAMES.values():
            self.assertIn(known, message)

    def test_non_string_entry_raises_rather_than_being_skipped(self):
        """Unhashable and wrong-typed entries take the same path as unknown ones."""
        with self.assertRaises(ValueError):
            parse_state_filter([["active"]])

    def test_every_state_has_a_wire_name_and_round_trips(self):
        """Enumerated from the type, so a fourth state fails here rather than silently
        becoming unaddressable from the CLI."""
        for state in (StateFilter.ACTIVE, StateFilter.IDLE, StateFilter.PAUSED):
            with self.subTest(state=state):
                name = state_filter_name(state)
                self.assertEqual(parse_state_filter([name]), state)


class TestLifecycleState(unittest.TestCase):
    def test_plain_record_is_active(self):
        self.assertEqual(lifecycle_state(_record()), StateFilter.ACTIVE)

    def test_dropped_record_is_idle(self):
        self.assertEqual(
            lifecycle_state(_record(dropped_at="2026-08-16T00:00:00Z")),
            StateFilter.IDLE,
        )

    def test_paused_record_is_paused(self):
        self.assertEqual(lifecycle_state(_record(paused=True)), StateFilter.PAUSED)

    def test_paused_outranks_dropped(self):
        """A record that is both is still awaiting a human decision.

        Reporting it as idle would hide it from the default view, which is the
        one state an operator has to act on.
        """
        both = _record(paused=True, dropped_at="2026-08-16T00:00:00Z")
        self.assertEqual(lifecycle_state(both), StateFilter.PAUSED)

    def test_exactly_one_state_per_record(self):
        """The three answers partition the records — no record matches two."""
        for record in (
            _record(),
            _record(dropped_at="t"),
            _record(paused=True),
            _record(paused=True, dropped_at="t"),
        ):
            with self.subTest(record=record):
                matching = [
                    s
                    for s in (StateFilter.ACTIVE, StateFilter.IDLE, StateFilter.PAUSED)
                    if lifecycle_state(record) is s
                ]
                self.assertEqual(len(matching), 1)


class TestMergedView(unittest.TestCase):
    """The one statement of "which records exist" — shared by save and list."""

    def _store(self, disk: list[WatcherState]) -> StateStore:
        connector = MagicMock()
        connector.get_last_processed_ts = MagicMock(return_value=None)
        store = StateStore("rc-test", connector)
        store.load = MagicMock(return_value={ws.watcher_name: ws for ws in disk})
        return store

    def test_a_disk_record_the_caller_never_held_survives(self):
        """The case the merge exists for: a watcher whose agent was unavailable,
        or whose start raised, is on disk and absent from memory."""
        on_disk = _record("blocked", session_id="s-blocked")
        store = self._store([on_disk])

        view = store.merged_view({"healthy": _record("healthy")})

        self.assertEqual(set(view), {"blocked", "healthy"})
        self.assertEqual(view["blocked"].session_id, "s-blocked")

    def test_in_memory_wins_on_conflict(self):
        store = self._store([_record("w1", session_id="stale")])

        view = store.merged_view({"w1": _record("w1", session_id="fresh")})

        self.assertEqual(view["w1"].session_id, "fresh")

    def test_save_writes_exactly_what_the_view_returns(self):
        """If the two ever disagree, an operator is shown a set that is not the
        set being persisted."""
        store = self._store([_record("only-on-disk")])
        in_memory = {"in-memory": _record("in-memory")}
        expected = set(store.merged_view(in_memory))

        with unittest.mock.patch("gateway.core.state_store.save_state") as saved:
            store.save(in_memory)

        written = {ws.watcher_name for ws in saved.call_args[0][1]}
        self.assertEqual(written, expected)


if __name__ == "__main__":
    unittest.main()
