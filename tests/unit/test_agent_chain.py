"""Tests for agent-chain turn tracking and loop protection.

Covers:
  - TurnStore: check_and_increment within budget
  - TurnStore: check_and_increment at budget limit → denied
  - TurnStore: reset_sender allows fresh start
  - TurnStore: reset_all resets all senders for a thread
  - TurnStore: human message (non-agent) triggers reset_all via filter
  - TurnStore: TTL GC removes expired entries
  - build_agent_chain_context: normal turn
  - build_agent_chain_context: penultimate turn has warning
  - build_agent_chain_context: final turn has closing + scheduler hint
  - filter_rc_message: agent sender passes through with is_agent_chain=True
  - filter_rc_message: agent sender at turn limit → dropped + counter reset
  - filter_rc_message: require_mention=False allows non-mentioned messages
  - filter_rc_message: filter_sender=False allows unknown senders
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from gateway.connectors.rocketchat.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN, TurnStore
from gateway.connectors.rocketchat.config import AgentChainConfig, RocketChatConfig
from gateway.connectors.rocketchat.normalize import filter_rc_message
from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN as CORE_TOKEN
from gateway.core.agent_chain import build_agent_chain_context

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_config(
    username: str = "bot",
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    agent_usernames: list[str] | None = None,
    max_turns: int = 5,
    require_mention: bool = True,
    filter_sender: bool = True,
) -> RocketChatConfig:
    return RocketChatConfig(
        server_url="http://rc.test",
        username=username,
        password="secret",
        owners=owners or [],
        guests=guests or [],
        require_mention=require_mention,
        filter_sender=filter_sender,
        agent_chain=AgentChainConfig(
            agent_usernames=agent_usernames or [],
            max_turns=max_turns,
        ),
    )


def _make_doc(
    sender: str = "user1",
    rid: str = "room1",
    msg: str = "hello",
    tmid: str | None = None,
    mentions: list[dict] | None = None,
    ts: int = 1000,
) -> dict:
    doc: dict = {
        "u": {"username": sender, "_id": sender, "name": sender},
        "rid": rid,
        "msg": msg,
        "ts": ts,
    }
    if tmid:
        doc["tmid"] = tmid
    if mentions is not None:
        doc["mentions"] = mentions
    return doc


# ── TurnStore tests ────────────────────────────────────────────────────────────


class TestTurnStore(unittest.TestCase):
    def test_check_and_increment_within_budget(self):
        store = TurnStore()
        allowed, turn = store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

        allowed2, turn2 = store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertTrue(allowed2)
        self.assertEqual(turn2, 2)

    def test_check_and_increment_at_budget_limit_denied(self):
        store = TurnStore()
        for _ in range(3):
            store.check_and_increment("room1", None, "agentA", max_turns=3)

        allowed, turn = store.check_and_increment("room1", None, "agentA", max_turns=3)
        self.assertFalse(allowed)
        self.assertEqual(turn, 3)  # current count (not incremented)

    def test_reset_sender_allows_fresh_start(self):
        store = TurnStore()
        for _ in range(3):
            store.check_and_increment("room1", None, "agentA", max_turns=3)

        store.reset_sender("room1", None, "agentA")

        allowed, turn = store.check_and_increment("room1", None, "agentA", max_turns=3)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

    def test_reset_all_resets_all_senders_for_thread(self):
        store = TurnStore()
        store.check_and_increment("room1", "thread1", "agentA", max_turns=5)
        store.check_and_increment("room1", "thread1", "agentB", max_turns=5)
        store.check_and_increment("room1", None, "agentA", max_turns=5)  # different thread

        store.reset_all("room1", "thread1")

        # agentA and agentB in thread1 reset
        self.assertEqual(store.current_turns("room1", "thread1", "agentA"), 0)
        self.assertEqual(store.current_turns("room1", "thread1", "agentB"), 0)
        # agentA in top-level (None thread) not affected
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

    def test_reset_all_on_human_message_via_filter(self):
        """Non-agent message passing through filter triggers reset_all for that context."""
        store = TurnStore()
        store.check_and_increment("room1", None, "agentA", max_turns=5)

        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        doc = _make_doc(sender="human1", rid="room1", msg="@bot hi")

        # Human message in DM (room_type=dm) passes sender filter, triggers reset_all
        filter_rc_message(doc, config, "dm", None, turn_store=store)

        self.assertEqual(store.current_turns("room1", None, "agentA"), 0)

    def test_ttl_gc_removes_expired_entries(self):
        store = TurnStore(ttl_seconds=1.0)
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

        # Simulate time passing beyond TTL
        with patch("gateway.connectors.rocketchat.agent_chain.time.monotonic",
                   return_value=time.monotonic() + 2.0):
            store._gc()

        self.assertEqual(store.current_turns("room1", None, "agentA"), 0)

    def test_independent_counters_per_sender(self):
        store = TurnStore()
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        store.check_and_increment("room1", None, "agentB", max_turns=5)

        self.assertEqual(store.current_turns("room1", None, "agentA"), 2)
        self.assertEqual(store.current_turns("room1", None, "agentB"), 1)

    def test_independent_counters_per_thread(self):
        store = TurnStore()
        store.check_and_increment("room1", "t1", "agentA", max_turns=5)
        store.check_and_increment("room1", "t2", "agentA", max_turns=5)
        store.check_and_increment("room1", "t2", "agentA", max_turns=5)

        self.assertEqual(store.current_turns("room1", "t1", "agentA"), 1)
        self.assertEqual(store.current_turns("room1", "t2", "agentA"), 2)

    def test_current_turns_returns_zero_for_unknown(self):
        store = TurnStore()
        self.assertEqual(store.current_turns("room1", None, "nobody"), 0)


# ── build_agent_chain_context tests ───────────────────────────────────────────


class TestBuildAgentChainContext(unittest.TestCase):
    def test_normal_turn_includes_turn_info_and_termination_hint(self):
        ctx = build_agent_chain_context(turn=2, max_turns=5)
        self.assertIn("[Agent chain: turn 2/5]", ctx)
        self.assertIn(CORE_TOKEN, ctx)
        self.assertNotIn("final turn", ctx)
        self.assertNotIn("next response will be your last", ctx)

    def test_loop_detection_hint_present_on_non_final_turns(self):
        """Non-final turns include the loop-detection instruction."""
        for turn in (1, 2, 4):  # all turns before max
            ctx = build_agent_chain_context(turn=turn, max_turns=5)
            self.assertIn("repeating without making progress", ctx)

    def test_loop_detection_hint_absent_on_final_turn(self):
        """Final turn uses graceful-closing wording instead of loop hint."""
        ctx = build_agent_chain_context(turn=5, max_turns=5)
        self.assertNotIn("repeating without making progress", ctx)

    def test_penultimate_turn_has_warning(self):
        ctx = build_agent_chain_context(turn=4, max_turns=5)
        self.assertIn("[Agent chain: turn 4/5]", ctx)
        self.assertIn("next response will be your last", ctx)
        self.assertIn(CORE_TOKEN, ctx)

    def test_final_turn_has_closing_and_scheduler_hint(self):
        ctx = build_agent_chain_context(turn=5, max_turns=5)
        self.assertIn("[Agent chain: turn 5/5]", ctx)
        self.assertIn("final turn", ctx)
        self.assertIn("scheduler tool", ctx)
        # At max_turns, no termination token hint (can't self-terminate on last turn)
        self.assertNotIn(CORE_TOKEN, ctx)

    def test_first_turn_normal(self):
        ctx = build_agent_chain_context(turn=1, max_turns=5)
        self.assertIn("[Agent chain: turn 1/5]", ctx)
        self.assertIn(CORE_TOKEN, ctx)
        self.assertNotIn("final", ctx)

    def test_tokens_match_between_core_and_connector(self):
        """The token defined in core and connector layers must be identical."""
        self.assertEqual(CORE_TOKEN, AGENT_CHAIN_TERMINATION_TOKEN)


# ── filter_rc_message agent chain tests ───────────────────────────────────────


class TestFilterRcMessageAgentChain(unittest.TestCase):
    def test_agent_sender_passes_with_is_agent_chain_true(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        store = TurnStore()
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)
        self.assertEqual(result.agent_chain_turn, 1)
        self.assertEqual(result.agent_chain_max_turns, 5)

    def test_agent_sender_at_turn_limit_dropped_counter_stays_at_max(self):
        """Counter is NOT reset on force-drop — stays at max to prevent loop restart."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()
        # Exhaust budget
        for i in range(3):
            doc = _make_doc(sender="agentA", rid="room1", msg="msg", ts=i + 1)
            filter_rc_message(doc, config, "channel", None, turn_store=store)

        # 4th message: force-drop, counter stays at max
        doc = _make_doc(sender="agentA", rid="room1", msg="over limit", ts=100)
        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "agent chain turn limit reached")
        # Counter stays at max (NOT reset) so subsequent messages stay blocked
        self.assertEqual(store.current_turns("room1", None, "agentA"), 3)

    def test_agent_sender_stays_blocked_after_turn_limit(self):
        """Once force-dropped, ALL subsequent messages are also dropped until reset."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()
        for i in range(3):
            doc = _make_doc(sender="agentA", rid="room1", msg="msg", ts=i + 1)
            filter_rc_message(doc, config, "channel", None, turn_store=store)

        # Every subsequent message is immediately force-dropped
        for ts in range(100, 105):
            doc = _make_doc(sender="agentA", rid="room1", msg="still trying", ts=ts)
            result = filter_rc_message(doc, config, "channel", None, turn_store=store)
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "agent chain turn limit reached")

    def test_agent_bypasses_mention_requirement_in_channel(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], require_mention=True)
        store = TurnStore()
        # No @mention in the message
        doc = _make_doc(sender="agentA", rid="room1", msg="no mention here")

        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)

    def test_agent_not_in_allow_list_still_accepted(self):
        """Agent usernames bypass the allow-list / filter_sender check."""
        config = _make_config(
            owners=["human1"],
            guests=[],
            agent_usernames=["agentA"],
            filter_sender=True,  # strict allow-list mode
        )
        store = TurnStore()
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None, turn_store=store)

        self.assertTrue(result.accepted)

    def test_require_mention_false_allows_non_mentioned_human_messages(self):
        config = _make_config(owners=["human1"], require_mention=False)
        doc = _make_doc(sender="human1", rid="room1", msg="no mention here")

        result = filter_rc_message(doc, config, "channel", None)

        self.assertTrue(result.accepted)
        self.assertFalse(result.is_agent_chain)

    def test_filter_sender_false_allows_unknown_senders(self):
        config = _make_config(owners=["human1"], filter_sender=False)
        doc = _make_doc(sender="stranger", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None)

        self.assertTrue(result.accepted)
        self.assertFalse(result.is_agent_chain)

    def test_filter_sender_true_blocks_unknown_senders(self):
        config = _make_config(owners=["human1"], filter_sender=True)
        doc = _make_doc(sender="stranger", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "sender not in allow-list")

    def test_agent_in_thread_tracked_independently(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()

        doc_t1 = _make_doc(sender="agentA", rid="room1", msg="msg", tmid="thread1", ts=1)
        doc_t2 = _make_doc(sender="agentA", rid="room1", msg="msg", tmid="thread2", ts=2)

        r1 = filter_rc_message(doc_t1, config, "channel", None, turn_store=store)
        r2 = filter_rc_message(doc_t2, config, "channel", None, turn_store=store)

        self.assertTrue(r1.accepted)
        self.assertTrue(r2.accepted)
        self.assertEqual(store.current_turns("room1", "thread1", "agentA"), 1)
        self.assertEqual(store.current_turns("room1", "thread2", "agentA"), 1)

    def test_no_turn_store_agent_passes_without_tracking(self):
        """When turn_store=None, agent messages pass without any turn tracking."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None, turn_store=None)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)
        self.assertEqual(result.agent_chain_turn, 0)  # no tracking done


if __name__ == "__main__":
    unittest.main()
