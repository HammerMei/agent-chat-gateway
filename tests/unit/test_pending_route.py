"""One copy of the buffer rule for both connectors (§2.2, §2.7 step 3)."""

import logging
import unittest

from gateway.core.pending_route import PendingRoute, route_attempts

logger = logging.getLogger("test-pending-route")


class TestPendingRoute(unittest.TestCase):
    def test_frames_buffer_and_drain_in_arrival_order(self):
        pending = PendingRoute(capacity=10)
        self.assertEqual(pending.add("m1", "f1"), "buffered")
        self.assertEqual(pending.add("m2", "f2"), "buffered")
        self.assertEqual(pending.drain(), ["f1", "f2"])
        self.assertEqual(len(pending), 0)

    def test_a_duplicate_id_is_discarded_without_disturbing_the_buffer(self):
        """§2.2 outcome 6 — and this set is the *only* duplicate guard during an
        episode: a brand-new room's subscription starts with empty seen-ids and
        an empty watermark, so a duplicate riding the buffer would be delivered
        twice after creation."""
        pending = PendingRoute(capacity=10)
        pending.add("m1", "f1")
        self.assertEqual(pending.add("m1", "f1-copy"), "duplicate")
        self.assertEqual(pending.drain(), ["f1"])

    def test_a_full_buffer_answers_full_and_keeps_what_it_holds(self):
        pending = PendingRoute(capacity=1)
        pending.add("m1", "f1")
        self.assertEqual(pending.add("m2", "f2"), "full")
        self.assertEqual(pending.drain(), ["f1"])

    def test_a_frame_with_no_id_is_buffered_not_refused(self):
        """Losing a real message over a missing id is the worse trade."""
        pending = PendingRoute(capacity=10)
        self.assertEqual(pending.add("", "f1"), "buffered")
        self.assertEqual(pending.add("", "f2"), "buffered")
        self.assertEqual(pending.drain(), ["f1", "f2"])

    def test_a_duplicate_of_a_drained_frame_is_still_remembered(self):
        """The seen-set survives the drain: the episode is one reservation, and
        a late copy of an already-delivered frame is still the same message."""
        pending = PendingRoute(capacity=10)
        pending.add("m1", "f1")
        pending.drain()
        self.assertEqual(pending.add("m1", "f1-copy"), "duplicate")


class TestRouteAttempts(unittest.IsolatedAsyncioTestCase):
    async def test_success_on_a_retry_answers_true(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")

        ok = await route_attempts(
            flaky, retry_on=RuntimeError, delays=(0,), logger=logger, label="x")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)

    async def test_exhausted_retries_park_with_false(self):
        async def dead():
            raise RuntimeError("permanent")

        ok = await route_attempts(
            dead, retry_on=RuntimeError, delays=(0, 0), logger=logger, label="x")
        self.assertFalse(ok)

    async def test_only_the_named_exception_is_retried(self):
        """A bug should surface as a bug, not spend three backoffs pretending
        to be weather."""
        calls = []

        async def buggy():
            calls.append(1)
            raise ValueError("a bug")

        with self.assertRaises(ValueError):
            await route_attempts(
                buggy, retry_on=RuntimeError, delays=(0, 0), logger=logger, label="x")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
