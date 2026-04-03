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

from gateway.agents.claude.adapter import (
    _MAX_RAW_PREVIEW_CHARS,
    _parse_intermediate_events,
    _StreamParser,
)


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


class TestParseIntermediateEvents(unittest.TestCase):
    """Tests for _parse_intermediate_events helper."""

    def _tool_use_line(self, name: str) -> str:
        return json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}]},
        })

    def _thinking_line(self, thinking: str) -> str:
        return json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": thinking}]},
        })

    def _text_line(self, text: str) -> str:
        return json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        })

    def test_tool_use_block_yields_tool_call_event(self):
        events = _parse_intermediate_events(self._tool_use_line("Bash"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "tool_call")
        self.assertEqual(events[0].text, "🔧 Bash")

    def test_thinking_block_yields_thinking_event(self):
        events = _parse_intermediate_events(self._thinking_line("Let me think"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "thinking")
        self.assertIn("💭", events[0].text)
        self.assertIn("Let me think", events[0].text)

    def test_thinking_block_truncated_at_80_chars(self):
        long_thought = "x" * 100
        events = _parse_intermediate_events(self._thinking_line(long_thought))
        self.assertEqual(len(events), 1)
        self.assertIn("...", events[0].text)
        # text should be 💭 + space + 80 chars + "..."
        self.assertLessEqual(len(events[0].text), 90)

    def test_text_block_returns_empty(self):
        events = _parse_intermediate_events(self._text_line("Hello world"))
        self.assertEqual(events, [])

    def test_result_event_returns_empty(self):
        line = json.dumps({"type": "result", "subtype": "success", "session_id": "s1"})
        events = _parse_intermediate_events(line)
        self.assertEqual(events, [])

    def test_user_event_returns_empty(self):
        line = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        })
        events = _parse_intermediate_events(line)
        self.assertEqual(events, [])

    def test_malformed_json_returns_empty(self):
        events = _parse_intermediate_events("{not valid json")
        self.assertEqual(events, [])

    def test_multiple_blocks_in_one_event(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
            ]},
        })
        events = _parse_intermediate_events(line)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].text, "🔧 Read")
        self.assertEqual(events[1].text, "🔧 Bash")

    def test_empty_thinking_block_ignored(self):
        events = _parse_intermediate_events(self._thinking_line(""))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
