"""Unit tests for gateway.admin._logging.quiet_expected_error."""

from __future__ import annotations

import logging
import unittest

from gateway.admin._logging import quiet_expected_error


class TestQuietExpectedError(unittest.TestCase):
    def test_raises_level_inside_block(self):
        logger = logging.getLogger("test.quiet.raises")
        logger.setLevel(logging.DEBUG)

        with quiet_expected_error(logger):
            self.assertEqual(logger.level, logging.CRITICAL)

    def test_restores_previous_level_after_block(self):
        logger = logging.getLogger("test.quiet.restores")
        logger.setLevel(logging.DEBUG)

        with quiet_expected_error(logger):
            pass

        self.assertEqual(logger.level, logging.DEBUG)

    def test_restores_previous_level_even_if_block_raises(self):
        logger = logging.getLogger("test.quiet.restores_on_error")
        logger.setLevel(logging.INFO)

        with self.assertRaises(ValueError):
            with quiet_expected_error(logger):
                raise ValueError("boom")

        self.assertEqual(logger.level, logging.INFO)

    def test_error_call_is_suppressed_inside_block(self):
        logger = logging.getLogger("test.quiet.suppresses_error")
        logger.setLevel(logging.DEBUG)

        with quiet_expected_error(logger):
            self.assertFalse(logger.isEnabledFor(logging.ERROR))

        self.assertTrue(logger.isEnabledFor(logging.ERROR))


if __name__ == "__main__":
    unittest.main()
