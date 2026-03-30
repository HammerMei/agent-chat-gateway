"""Tests for gateway/core/adapter_utils.py — build_attachment_prompt().

Run with:
    uv run python -m pytest tests/test_adapter_utils.py -v
"""

from __future__ import annotations

import unittest


class TestBuildAttachmentPrompt(unittest.TestCase):
    """build_attachment_prompt() injects file paths into the prompt."""

    def _build(self, prompt, attachments, cwd=None, instruction=None):
        from gateway.core.adapter_utils import build_attachment_prompt
        kwargs = {}
        if instruction is not None:
            kwargs["instruction"] = instruction
        return build_attachment_prompt(prompt, attachments, cwd, **kwargs)

    # ── No-op cases ───────────────────────────────────────────────────────────

    def test_none_attachments_returns_prompt_unchanged(self):
        result = self._build("Hello", None)
        self.assertEqual(result, "Hello")

    def test_empty_list_returns_prompt_unchanged(self):
        result = self._build("Hello", [])
        self.assertEqual(result, "Hello")

    def test_empty_prompt_with_no_attachments(self):
        result = self._build("", None)
        self.assertEqual(result, "")

    # ── Single attachment ─────────────────────────────────────────────────────

    def test_single_attachment_no_cwd_uses_full_path(self):
        """Without working_directory, the full path is shown as the label."""
        result = self._build("prompt", ["/tmp/docs/report.pdf"])
        self.assertIn("Attached:", result)
        self.assertIn("report.pdf", result)
        self.assertIn("/tmp/docs/report.pdf", result)

    def test_single_attachment_with_cwd_shows_relative_path(self):
        """When the file is inside working_directory, a relative path is shown."""
        result = self._build(
            "prompt",
            ["/workspace/project/data/file.txt"],
            cwd="/workspace/project",
        )
        self.assertIn("Attached:", result)
        self.assertIn("file.txt", result)
        self.assertIn("data/file.txt", result)
        # Full path should NOT appear when relative path is available
        self.assertNotIn("/workspace/project/data/file.txt", result)

    def test_single_attachment_outside_cwd_falls_back_to_absolute(self):
        """When the file is outside cwd, the absolute path is used."""
        result = self._build(
            "prompt",
            ["/other/path/report.pdf"],
            cwd="/workspace/project",
        )
        self.assertIn("/other/path/report.pdf", result)

    def test_default_instruction_is_use_read_tool(self):
        """The default hint tells the agent to use the Read tool."""
        result = self._build("prompt", ["/tmp/file.txt"])
        self.assertIn("Read tool", result)

    def test_custom_instruction(self):
        """A custom instruction replaces the default hint."""
        result = self._build(
            "prompt",
            ["/tmp/file.txt"],
            instruction="open it with the editor",
        )
        self.assertIn("open it with the editor", result)
        self.assertNotIn("Read tool", result)

    # ── Multiple attachments ──────────────────────────────────────────────────

    def test_multiple_attachments_each_on_own_line(self):
        """Each attachment produces a separate [Attached: ...] line."""
        result = self._build(
            "prompt",
            ["/tmp/a.txt", "/tmp/b.pdf", "/tmp/c.png"],
        )
        lines = result.split("\n")
        attached_lines = [l for l in lines if l.startswith("[Attached:")]
        self.assertEqual(len(attached_lines), 3)

    def test_multiple_attachments_all_filenames_present(self):
        result = self._build(
            "prompt",
            ["/workspace/img.png", "/workspace/report.pdf"],
            cwd="/workspace",
        )
        self.assertIn("img.png", result)
        self.assertIn("report.pdf", result)

    # ── Prompt structure ──────────────────────────────────────────────────────

    def test_original_prompt_preserved_as_prefix(self):
        """The original prompt text comes before the attachment notes."""
        result = self._build("What does this file say?", ["/tmp/file.txt"])
        self.assertTrue(
            result.startswith("What does this file say?"),
            f"Expected prompt at start, got: {result[:80]!r}",
        )

    def test_result_stripped_of_leading_trailing_whitespace(self):
        """The final result has no leading/trailing whitespace."""
        result = self._build("  hello  ", ["/tmp/file.txt"])
        self.assertEqual(result, result.strip())

    def test_empty_prompt_with_attachment(self):
        """An empty prompt with attachments returns only the attachment lines."""
        result = self._build("", ["/tmp/data.csv"])
        self.assertIn("Attached:", result)
        # No leading newline
        self.assertFalse(result.startswith("\n"))

    # ── Annotation format ─────────────────────────────────────────────────────

    def test_annotation_format_brackets_arrow(self):
        """Each annotation follows the [Attached: name → path — instruction] format."""
        result = self._build("p", ["/tmp/report.pdf"])
        # Should match [Attached: filename → label — instruction]
        self.assertRegex(result, r"\[Attached: report\.pdf → .+ — .+\]")

    def test_filename_extracted_correctly_from_nested_path(self):
        """Only the filename (not the full path) appears as the first part."""
        result = self._build("p", ["/very/deeply/nested/dir/myfile.txt"])
        # The name part before → should be just "myfile.txt"
        import re
        match = re.search(r"\[Attached: (.+?) →", result)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "myfile.txt")


# ── Tests: ts_to_float (Q4) ───────────────────────────────────────────────────


class TestTsToFloat(unittest.TestCase):
    """Q4: ts_to_float — single source of truth for timestamp parsing."""

    def _f(self, ts):
        from gateway.core.adapter_utils import ts_to_float
        return ts_to_float(ts)

    def test_numeric_string_parsed(self):
        self.assertEqual(self._f("1711234567890"), 1711234567890.0)

    def test_float_string_parsed(self):
        self.assertAlmostEqual(self._f("1711234567.123"), 1711234567.123)

    def test_none_returns_none(self):
        self.assertIsNone(self._f(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._f(""))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(self._f("not-a-ts"))

    def test_iso8601_returns_none(self):
        # ISO-8601 strings are not parseable as float
        self.assertIsNone(self._f("2024-01-01T00:00:00Z"))


# ── Tests: ts_gt (Q4) ─────────────────────────────────────────────────────────


class TestTsGt(unittest.TestCase):
    """Q4: ts_gt — numeric timestamp comparison with lexicographic fallback."""

    def _gt(self, a, b):
        from gateway.core.adapter_utils import ts_gt
        return ts_gt(a, b)

    def test_larger_numeric_ts_is_greater(self):
        self.assertTrue(self._gt("1711234567891", "1711234567890"))

    def test_equal_numeric_ts_is_not_greater(self):
        self.assertFalse(self._gt("1711234567890", "1711234567890"))

    def test_smaller_numeric_ts_is_not_greater(self):
        self.assertFalse(self._gt("1711234567889", "1711234567890"))

    def test_numeric_takes_precedence_over_string_length(self):
        # "9" < "10" lexicographically but 9 < 10 numerically —
        # ts_gt must use numeric comparison.
        self.assertFalse(self._gt("9", "10"))
        self.assertTrue(self._gt("10", "9"))

    def test_non_numeric_falls_back_to_lexicographic(self):
        # When both values can't be parsed as float, fall back to str comparison
        self.assertTrue(self._gt("b", "a"))
        self.assertFalse(self._gt("a", "b"))

    def test_mixed_numeric_and_non_numeric_falls_back(self):
        # One side is parseable, the other is not — falls back to str comparison
        # (ts_to_float returns None for non-numeric side)
        self.assertFalse(self._gt("100", "not-a-ts"))  # "100" < "not-a-ts" lexicographically

    def test_float_strings_compared_correctly(self):
        self.assertTrue(self._gt("1711234567.9", "1711234567.1"))


if __name__ == "__main__":
    unittest.main()
