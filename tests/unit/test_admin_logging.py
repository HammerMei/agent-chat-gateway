"""Unit tests for gateway.admin._logging.quiet_expected_error.

Covers both the direct behavior (suppresses log records inside the block,
restores normal behavior after) and the concurrency property that motivated
switching from a shared logger.setLevel() to a task-local contextvars.ContextVar
+ logging.Filter: two overlapping quiet_expected_error() blocks on different
asyncio.Tasks must not interfere with each other, and must not leave the
logger permanently muted after both exit.
"""

from __future__ import annotations

import asyncio
import logging
import unittest

from gateway.admin._logging import quiet_expected_error


def _make_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.filters = []  # isolate from any filter a previous test attached
    logger.setLevel(logging.DEBUG)
    return logger


class _ListHandler(logging.Handler):
    """Captures emitted records instead of writing anywhere."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestQuietExpectedError(unittest.TestCase):
    def test_suppresses_error_inside_block(self):
        logger = _make_logger("test.quiet.suppresses")
        handler = _ListHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        with quiet_expected_error(logger):
            logger.error("boom")

        self.assertEqual(handler.records, [])

    def test_does_not_suppress_before_or_after_block(self):
        logger = _make_logger("test.quiet.restores")
        handler = _ListHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        logger.error("before")
        with quiet_expected_error(logger):
            pass
        logger.error("after")

        self.assertEqual(len(handler.records), 2)

    def test_suppression_lifts_even_if_block_raises(self):
        logger = _make_logger("test.quiet.raises")
        handler = _ListHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        with self.assertRaises(ValueError):
            with quiet_expected_error(logger):
                raise ValueError("boom")
        logger.error("after")

        self.assertEqual(len(handler.records), 1)

    def test_does_not_mutate_logger_level(self):
        # The whole point of the contextvars-based redesign: no shared
        # mutable state on the logger object that concurrent callers could
        # race on. Level is left alone entirely.
        logger = _make_logger("test.quiet.level")
        logger.setLevel(logging.INFO)

        with quiet_expected_error(logger):
            self.assertEqual(logger.level, logging.INFO)

        self.assertEqual(logger.level, logging.INFO)

    def test_filter_is_not_duplicated_on_repeated_calls(self):
        logger = _make_logger("test.quiet.no_dup_filter")

        with quiet_expected_error(logger):
            pass
        with quiet_expected_error(logger):
            pass

        self.assertEqual(len(logger.filters), 1)


class TestQuietExpectedErrorConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_tasks_do_not_interfere(self):
        """Regression test for the exact race the old setLevel()-based
        implementation had: task A enters, task B enters (observes A's
        suppressed state), A exits, B exits — B must NOT permanently
        re-suppress the logger for everyone after both have finished."""
        logger = _make_logger("test.quiet.concurrency")
        handler = _ListHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        a_entered = asyncio.Event()
        b_entered = asyncio.Event()
        a_may_exit = asyncio.Event()

        async def task_a():
            with quiet_expected_error(logger):
                logger.error("from A, should be suppressed")
                a_entered.set()
                await a_may_exit.wait()

        async def task_b():
            await a_entered.wait()
            with quiet_expected_error(logger):
                logger.error("from B, should be suppressed")
                b_entered.set()
                a_may_exit.set()  # let A exit while B is still inside its block
                await asyncio.sleep(0)  # yield so A's exit runs before B's

        await asyncio.gather(task_a(), task_b())

        # Both suppressed while inside their own blocks.
        self.assertEqual(handler.records, [])

        # Critically: after BOTH tasks have exited, the logger must be back
        # to normal — not permanently muted by B's exit observing A's
        # already-active suppression.
        logger.error("after both tasks finished")
        self.assertEqual(len(handler.records), 1)


if __name__ == "__main__":
    unittest.main()
