"""Tests for _StreamParser (Claude incremental stream-json parser, P2-9).

Covers:
  - Single assistant event → text extracted
  - Multiple assistant events → text concatenated
  - Result event → metadata (session_id, cost, usage) extracted
  - Fallback text from result when no assistant blocks
  - Empty input → "(empty response)"
  - Malformed JSON lines → silently skipped
  - Raw preview bounded for diagnostics
  - is_error propagated from result event
"""

from __future__ import annotations

import json
import unittest

from gateway.agents.claude.adapter import _MAX_RAW_PREVIEW_CHARS, _StreamParser


def _assistant_line(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _result_line(
    session_id: str = "sess-1",
    is_error: bool = False,
    subtype: str = "success",
    result: str = "",
    cost: float | None = None,
    usage: dict | None = None,
) -> str:
    event: dict = {
        "type": "result",
        "subtype": subtype,
        "session_id": session_id,
        "is_error": is_error,
    }
    if result:
        event["result"] = result
    if cost is not None:
        event["total_cost_usd"] = cost
    if usage:
        event["usage"] = usage
    return json.dumps(event)


class TestStreamParser(unittest.TestCase):

    def test_single_assistant_event(self):
        parser = _StreamParser()
        parser.feed_line(_assistant_line("Hello world"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "Hello world")

    def test_multiple_assistant_events_concatenated(self):
        parser = _StreamParser()
        parser.feed_line(_assistant_line("Line one"))
        parser.feed_line(_assistant_line("Line two"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "Line one\nLine two")

    def test_result_event_extracts_metadata(self):
        parser = _StreamParser()
        parser.feed_line(_assistant_line("reply"))
        parser.feed_line(_result_line(
            session_id="sess-42",
            cost=0.0123,
            usage={"input_tokens": 100, "output_tokens": 50},
        ))
        resp = parser.build_response()
        self.assertEqual(resp.session_id, "sess-42")
        self.assertAlmostEqual(resp.cost_usd, 0.0123)
        self.assertIsNotNone(resp.usage)
        self.assertEqual(resp.usage.input_tokens, 100)
        self.assertEqual(resp.usage.output_tokens, 50)

    def test_fallback_text_from_result_when_no_assistant(self):
        """Result.result is used as fallback when no assistant blocks are present."""
        parser = _StreamParser()
        parser.feed_line(_result_line(result="fallback text"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "fallback text")

    def test_assistant_text_takes_priority_over_result_fallback(self):
        parser = _StreamParser()
        parser.feed_line(_assistant_line("primary"))
        parser.feed_line(_result_line(result="fallback"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "primary")

    def test_empty_input_returns_placeholder(self):
        parser = _StreamParser()
        resp = parser.build_response()
        self.assertEqual(resp.text, "(empty response)")

    def test_malformed_json_silently_skipped(self):
        parser = _StreamParser()
        parser.feed_line("{not valid json")
        parser.feed_line(_assistant_line("good line"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "good line")

    def test_blank_lines_ignored(self):
        parser = _StreamParser()
        parser.feed_line("")
        parser.feed_line("  ")
        parser.feed_line(_assistant_line("text"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "text")

    def test_is_error_propagated(self):
        parser = _StreamParser()
        parser.feed_line(_result_line(is_error=True, result="error msg"))
        resp = parser.build_response()
        self.assertTrue(resp.is_error)
        self.assertEqual(resp.text, "error msg")

    def test_raw_preview_bounded(self):
        """_raw_preview does not grow beyond _MAX_RAW_PREVIEW_CHARS."""
        parser = _StreamParser()
        # Feed many long lines
        for i in range(100):
            parser.feed_line(_assistant_line("x" * 200))
        self.assertLessEqual(len(parser._raw_preview), _MAX_RAW_PREVIEW_CHARS)

    def test_non_assistant_non_result_events_ignored(self):
        parser = _StreamParser()
        parser.feed_line(json.dumps({"type": "system", "data": "init"}))
        parser.feed_line(_assistant_line("real text"))
        resp = parser.build_response()
        self.assertEqual(resp.text, "real text")

    def test_usage_cache_tokens_extracted(self):
        parser = _StreamParser()
        parser.feed_line(_assistant_line("text"))
        parser.feed_line(_result_line(usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 20,
        }))
        resp = parser.build_response()
        self.assertEqual(resp.usage.cache_read_tokens, 80)
        self.assertEqual(resp.usage.cache_write_tokens, 20)


if __name__ == "__main__":
    unittest.main()
