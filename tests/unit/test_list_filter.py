"""`list`'s state filter, its state derivation, and the merged record view.

Design §2.8: `list` enumerates persisted records, not config entries and not
live processors — so the three pieces that decide *which* records it returns
are tested here, and the rows themselves in
``tests/integration/test_list_watchers.py``.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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

    def test_the_default_is_everything_except_idle(self):
        """Pinned against the members rather than a literal list, so adding a
        state forces a decision about whether it is operable."""
        self.assertEqual(parse_state_filter(None), StateFilter.ALL & ~StateFilter.IDLE)

    def test_names_compose(self):
        self.assertEqual(
            parse_state_filter(["active", "idle"]),
            StateFilter.ACTIVE | StateFilter.IDLE,
        )

    def test_every_name_together_equals_all(self):
        """Derived from the type: a new state that `--all` cannot name fails here."""
        self.assertEqual(
            parse_state_filter(list(STATE_FILTER_NAMES.values())), StateFilter.ALL
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
        """Enumerated from ``StateFilter`` itself, not from a copy of its members.

        A ``Flag`` iterates its canonical single-bit members only, so composites
        like OPERABLE are excluded automatically — which means a fifth state
        added without a wire name fails here rather than silently becoming
        unaddressable from the CLI.
        """
        for state in StateFilter:
            with self.subTest(state=state):
                name = state_filter_name(state)
                self.assertEqual(parse_state_filter([name]), state)


class TestLifecycleState(unittest.TestCase):
    """Four answers, and the order between them is the design (§2.5)."""

    def test_a_resident_record_is_active(self):
        self.assertEqual(
            lifecycle_state(_record(), resident=True), StateFilter.ACTIVE
        )

    def test_a_record_that_wants_to_be_resident_and_is_not_is_failed(self):
        """What a start that wrote its record and then raised leaves behind."""
        self.assertEqual(
            lifecycle_state(_record(), resident=False), StateFilter.FAILED
        )

    def test_dropped_record_is_idle_even_though_it_is_not_resident(self):
        """An idle record is *supposed* to have no processor.

        This is why `dropped_at` is checked before residency — the reverse order
        would report every idle watcher as failed.

        Note the fixture: nothing in the gateway writes `dropped_at` yet (the
        watcher manager will, §2.5), so this state is constructed by hand here
        and `--idle` returns nothing in production today.
        """
        self.assertEqual(
            lifecycle_state(_record(dropped_at="2026-08-16T00:00:00Z"), resident=False),
            StateFilter.IDLE,
        )

    def test_paused_record_is_paused(self):
        self.assertEqual(
            lifecycle_state(_record(paused=True), resident=False), StateFilter.PAUSED
        )

    def test_paused_outranks_dropped(self):
        """A record that is both is still awaiting a human decision.

        Reporting it as idle would hide it from the default view, which is the
        one state an operator has to act on.
        """
        both = _record(paused=True, dropped_at="2026-08-16T00:00:00Z")
        self.assertEqual(lifecycle_state(both, resident=False), StateFilter.PAUSED)

    def test_paused_outranks_residency(self):
        """A paused watcher that somehow still has a processor is still paused —
        §4.4: an explicit pause is never overridden by inference."""
        self.assertEqual(
            lifecycle_state(_record(paused=True), resident=True), StateFilter.PAUSED
        )

    def test_every_answer_is_a_single_state(self):
        """A composite return would put one record in two filters at once.

        Membership (``in``) rather than identity, so a composite genuinely
        matches more than one and the assertion can fail the way it claims to.
        """
        for record in (
            _record(),
            _record(dropped_at="t"),
            _record(paused=True),
            _record(paused=True, dropped_at="t"),
        ):
            for resident in (True, False):
                with self.subTest(record=record, resident=resident):
                    answer = lifecycle_state(record, resident=resident)
                    matching = [s for s in StateFilter if s in answer]
                    self.assertEqual(len(matching), 1, f"{answer!r} is not one state")


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

    def test_save_writes_both_halves_of_the_merge(self):
        """Asserted against the literal expectation, not against another call to
        the method under test — comparing `merged_view` with itself would pass
        even if it dropped the disk half entirely."""
        store = self._store([_record("only-on-disk")])

        with patch("gateway.core.state_store.save_state") as saved:
            store.save({"in-memory": _record("in-memory")})

        written = {ws.watcher_name for ws in saved.call_args[0][1]}
        self.assertEqual(written, {"only-on-disk", "in-memory"})


if __name__ == "__main__":
    unittest.main()
