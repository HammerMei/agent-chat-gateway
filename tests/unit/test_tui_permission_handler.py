"""Unit tests for gateway.tools.tui_permission_handler.tui_permission_handler.

Mocks out rich console output and the blocking input() call so tests run
non-interactively in CI.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch


class TestTuiPermissionHandler(unittest.IsolatedAsyncioTestCase):
    """Test the interactive permission handler with mocked I/O."""

    def _mock_console(self):
        """Patch rich console so it doesn't write to stdout during tests."""
        console = MagicMock()
        return patch("gateway.tools.tui_permission_handler._console", console)

    async def _call(self, inputs: list[str], tool_name: str = "Bash",
                    tool_input: dict | None = None) -> bool:
        """Call the handler with canned input() responses."""
        from gateway.tools.tui_permission_handler import tui_permission_handler

        input_iter = iter(inputs)

        def fake_input(_prompt=""):
            return next(input_iter)

        loop = asyncio.get_running_loop()

        async def fake_executor(_executor, func, *args):
            return fake_input(*args)

        with self._mock_console():
            with patch.object(loop, "run_in_executor", fake_executor):
                return await tui_permission_handler(
                    tool_name, tool_input if tool_input is not None else {}
                )

    async def test_y_returns_true(self):
        result = await self._call(["y"])
        self.assertTrue(result)

    async def test_yes_returns_true(self):
        result = await self._call(["yes"])
        self.assertTrue(result)

    async def test_n_returns_false(self):
        result = await self._call(["n"])
        self.assertFalse(result)

    async def test_no_returns_false(self):
        result = await self._call(["no"])
        self.assertFalse(result)

    async def test_invalid_then_y_loops(self):
        """Invalid answers cause it to re-prompt until valid input arrives."""
        result = await self._call(["maybe", "dunno", "y"])
        self.assertTrue(result)

    async def test_invalid_then_n_loops(self):
        result = await self._call(["what", "n"])
        self.assertFalse(result)

    async def test_eoferror_denies(self):
        """EOF (non-interactive pipe) → deny as safe default."""
        from gateway.tools.tui_permission_handler import tui_permission_handler

        loop = asyncio.get_running_loop()

        async def fake_executor(_executor, func, *args):
            raise EOFError

        with self._mock_console():
            with patch.object(loop, "run_in_executor", fake_executor):
                result = await tui_permission_handler("Read", {})
        self.assertFalse(result)

    async def test_with_tool_input_dict(self):
        result = await self._call(["y"], tool_name="Write",
                                  tool_input={"path": "/tmp/x.txt", "content": "hello"})
        self.assertTrue(result)

    async def test_with_empty_tool_input(self):
        """Empty tool_input renders '(no parameters)' branch."""
        result = await self._call(["n"], tool_name="Glob", tool_input={})
        self.assertFalse(result)

    async def test_with_long_tool_input_truncated(self):
        """Very long inputs are truncated to 800 chars."""
        result = await self._call(["y"], tool_name="Bash",
                                  tool_input={"cmd": "x" * 2000})
        self.assertTrue(result)

    async def test_case_insensitive(self):
        result = await self._call(["Y"])
        self.assertTrue(result)

    async def test_whitespace_stripped(self):
        result = await self._call(["  n  "])
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
