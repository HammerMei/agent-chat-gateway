"""Tests for gateway.core.prompt_builder.build_prompt().

Extracted from MessageProcessor to make prompt logic independently testable.
"""

import unittest

from gateway.core.prompt_builder import build_prompt


class TestBuildPrompt(unittest.TestCase):

    def test_text_only(self):
        self.assertEqual(build_prompt("hello", ""), "hello")

    def test_text_with_prefix(self):
        self.assertEqual(build_prompt("hello", "from: alice"), "from: alice hello")

    def test_text_with_none_prefix(self):
        """Empty/falsy prefix produces text only (no leading space)."""
        self.assertEqual(build_prompt("hello", ""), "hello")

    def test_text_with_warnings(self):
        result = build_prompt("hello", "", ["warn1", "warn2"])
        self.assertEqual(result, "hello\nwarn1\nwarn2")

    def test_prefix_and_warnings(self):
        result = build_prompt("hello", "from: alice", ["attachment too large"])
        self.assertEqual(result, "from: alice hello\nattachment too large")

    def test_empty_warnings_list_ignored(self):
        result = build_prompt("hello", "pfx", [])
        self.assertEqual(result, "pfx hello")

    def test_none_warnings_ignored(self):
        result = build_prompt("hello", "pfx", None)
        self.assertEqual(result, "pfx hello")

    def test_whitespace_stripped_from_prefix_join(self):
        """Prefix with trailing space doesn't produce double space."""
        result = build_prompt("hello", "prefix:")
        self.assertEqual(result, "prefix: hello")

    def test_empty_text_with_prefix(self):
        """Edge case: empty text body with a prefix."""
        result = build_prompt("", "from: alice")
        self.assertEqual(result, "from: alice")


if __name__ == "__main__":
    unittest.main()
