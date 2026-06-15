"""Regression tests for bundled system context docs."""

from pathlib import Path


def test_rc_context_contains_directed_reply_etiquette():
    context_path = Path(__file__).parents[2] / "gateway" / "contexts" / "rc-gateway-context.md"
    text = context_path.read_text(encoding="utf-8")

    assert "`to: @all`" in text
    assert "intentional broader fan-out" in text
    assert "priority responders" in text
    assert "PROHIBITED: Unsolicited Agent-Chain Replies" in text
    assert "Use explicit @mentions for directed replies" in text
    assert "human or agent" in text
    assert "directed at someone else as not addressed to you" in text
    assert "Do not choose the reply target from `from:` alone" in text
    assert "scheduler" in text
    assert "usually the original sender" in text
    assert "ONLY `<end-of-agent-chain>`" in text
    assert "Do not reply just to summarize another agent" in text
    assert "Scheduled A2A tasks must also be addressed" in text
