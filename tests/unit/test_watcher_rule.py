"""Tests for watcher rules: the rule shape, its parser, and shadow detection.

Covers `gateway.core.watcher_rule` and the rule half of `gateway.config`. The
static (`room:`/`rooms: [...]`) parser is deliberately untouched by this work and
is covered by `test_config_loading.py`; the two shapes coexist until the watcher
manager lands, so a few tests here assert that coexistence explicitly.

Run with:
    uv run python -m pytest tests/unit/test_watcher_rule.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from gateway.config import (
    _parse_one_watcher_rule,
    entry_is_watcher_rule,
    find_shadowed_rules,
)
from gateway.core.config import AgentConfig, ConnectorConfig
from gateway.core.room_pattern import RoomPattern
from gateway.core.watcher_rule import RoomKind, RoomMatcher, RuleMatch, WatcherRule

CONNECTORS = [
    ConnectorConfig(name="mm-home", type="mattermost", raw={}),
    ConnectorConfig(name="rc-home", type="rocketchat", raw={}),
]
CONNECTOR_NAMES = {c.name for c in CONNECTORS}
AGENTS = {"claude-eng": AgentConfig(name="claude-eng"), "claude-ops": AgentConfig(name="claude-ops")}


def parse(entry, *, index=0, templates=None, seen=None) -> WatcherRule:
    return _parse_one_watcher_rule(
        entry,
        index,
        connectors=CONNECTORS,
        connector_names=CONNECTOR_NAMES,
        agents=AGENTS,
        default_agent="claude-eng",
        config_dir=Path("/tmp"),
        templates=templates or {},
        seen_rule_names=seen if seen is not None else set(),
    )


def rule(name="r", connector="mm-home", **rooms) -> WatcherRule:
    return WatcherRule(
        name=name,
        connector=connector,
        agent="claude-eng",
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in rooms.get("include", ())),
            exclude=tuple(RoomPattern(p) for p in rooms.get("exclude", ())),
            direct=rooms.get("direct", False),
            group_direct=rooms.get("group_direct", False),
        ),
    )


MINIMAL = {"name": "eng", "connector": "mm-home", "rooms": {"include": ["eng-*"]}}


class TestShapeDiscrimination(unittest.TestCase):
    """The two shapes must be distinguishable without heuristics."""

    def test_mapping_rooms_is_a_rule(self):
        self.assertTrue(entry_is_watcher_rule({"rooms": {"include": ["a"]}}))

    def test_list_rooms_is_the_old_shorthand(self):
        self.assertFalse(entry_is_watcher_rule({"rooms": ["a", "b"]}))

    def test_single_room_is_static(self):
        self.assertFalse(entry_is_watcher_rule({"room": "a"}))

    def test_an_empty_rooms_mapping_still_reads_as_a_rule(self):
        """So it gets the rule parser's error message, not the static one's."""
        self.assertTrue(entry_is_watcher_rule({"rooms": {}}))

    def test_non_mappings_are_not_rules(self):
        for entry in ([], "x", None, 3):
            with self.subTest(entry=entry):
                self.assertFalse(entry_is_watcher_rule(entry))


class TestMatcherSemantics(unittest.TestCase):
    def test_include_claims(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("eng-backend", RoomKind.CHANNEL), RuleMatch.CLAIMED)

    def test_no_include_match_falls_through(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("ops-x", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_exclude_declines_rather_than_falling_through(self):
        """§2.1's real decision: an excluded room does NOT reach a later rule.
        Fall-through would make exclude a routing operator and let two rules
        contend for the same room."""
        m = RoomMatcher(
            include=(RoomPattern("eng-*"),), exclude=(RoomPattern("eng-archive"),)
        )
        self.assertIs(m.match("eng-archive", RoomKind.CHANNEL), RuleMatch.DECLINED)
        self.assertIsNot(m.match("eng-archive", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_groups_are_matched_by_name_like_channels(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("eng-secret", RoomKind.GROUP), RuleMatch.CLAIMED)

    def test_dms_need_the_opt_in_and_ignore_patterns(self):
        by_name = RoomMatcher(include=(RoomPattern("*"),))
        self.assertIs(by_name.match("anything", RoomKind.DM), RuleMatch.NO_MATCH)
        self.assertIs(RoomMatcher(direct=True).match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_group_dm_is_a_separate_opt_in_from_dm(self):
        """§6.4: require_mention is skipped for a 1:1 DM but must not be for a
        group DM, so `direct` must not imply `group_direct`."""
        self.assertIs(
            RoomMatcher(direct=True).match("", RoomKind.GROUP_DM), RuleMatch.NO_MATCH
        )
        self.assertIs(
            RoomMatcher(group_direct=True).match("", RoomKind.GROUP_DM),
            RuleMatch.CLAIMED,
        )
        self.assertIs(
            RoomMatcher(group_direct=True).match("", RoomKind.DM), RuleMatch.NO_MATCH
        )

    def test_exclude_is_not_consulted_for_dms(self):
        m = RoomMatcher(
            include=(RoomPattern("eng-*"),), exclude=(RoomPattern("*"),), direct=True
        )
        self.assertIs(m.match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_patterns_and_dm_opt_in_coexist_on_one_rule(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),), direct=True)
        self.assertIs(m.match("eng-x", RoomKind.CHANNEL), RuleMatch.CLAIMED)
        self.assertIs(m.match("", RoomKind.DM), RuleMatch.CLAIMED)
        self.assertIs(m.match("ops-x", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_room_kind_is_direct_helper(self):
        self.assertTrue(RoomKind.DM.is_direct)
        self.assertTrue(RoomKind.GROUP_DM.is_direct)
        self.assertFalse(RoomKind.CHANNEL.is_direct)
        self.assertFalse(RoomKind.GROUP.is_direct)


class TestParserHappyPath(unittest.TestCase):
    def test_minimal_rule(self):
        r = parse(MINIMAL)
        self.assertEqual(r.name, "eng")
        self.assertEqual(r.connector, "mm-home")
        self.assertEqual(r.agent, "claude-eng")  # default_agent
        self.assertIs(r.match("eng-x", RoomKind.CHANNEL), RuleMatch.CLAIMED)

    def test_name_is_stripped(self):
        self.assertEqual(parse({**MINIMAL, "name": "  eng  "}).name, "eng")

    def test_connector_defaults_to_the_first(self):
        entry = {"name": "eng", "rooms": {"include": ["eng-*"]}}
        self.assertEqual(parse(entry).connector, "mm-home")

    def test_explicit_agent(self):
        self.assertEqual(parse({**MINIMAL, "agent": "claude-ops"}).agent, "claude-ops")

    def test_dm_only_rule_needs_no_include(self):
        r = parse({"name": "dms", "connector": "mm-home", "rooms": {"direct": True}})
        self.assertIs(r.match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_ttls_are_read_and_live_on_the_rule(self):
        r = parse({**MINIMAL, "session_idle_days": 7, "session_expire_days": 30})
        self.assertEqual((r.session_idle_days, r.session_expire_days), (7, 30))

    def test_ttls_default_to_none_meaning_no_lifecycle(self):
        r = parse(MINIMAL)
        self.assertIsNone(r.session_idle_days)
        self.assertIsNone(r.session_expire_days)

    def test_notifications_and_history_handoff(self):
        r = parse({**MINIMAL, "online_notification": "hi", "history_handoff": {"fetch_count": 5}})
        self.assertEqual(r.online_notification, "hi")
        self.assertIsNone(r.offline_notification)
        self.assertEqual(r.history_handoff.fetch_count, 5)

    def test_inherits_supplies_shared_fields(self):
        templates = {"base": {"agent": "claude-ops", "session_idle_days": 3}}
        r = parse({**MINIMAL, "inherits": "base"}, templates=templates)
        self.assertEqual(r.agent, "claude-ops")
        self.assertEqual(r.session_idle_days, 3)

    def test_the_entrys_own_fields_win_over_the_template(self):
        templates = {"base": {"agent": "claude-ops"}}
        r = parse({**MINIMAL, "inherits": "base", "agent": "claude-eng"}, templates=templates)
        self.assertEqual(r.agent, "claude-eng")


class TestParserHardErrors(unittest.TestCase):
    def _err(self, entry, needle, **kw):
        with self.assertRaises(ValueError) as cm:
            parse(entry, **kw)
        self.assertIn(needle, str(cm.exception))
        return str(cm.exception)

    def test_missing_name_is_rejected_with_a_reason(self):
        msg = self._err({"connector": "mm-home", "rooms": {"include": ["a"]}}, "'name' is required")
        self.assertIn("no single room to derive a name from", msg)

    def test_blank_or_non_string_name(self):
        for bad in ("", "   ", 3, ["x"], None):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "name": bad}, "'name' is required")

    def test_duplicate_rule_name(self):
        seen = set()
        parse(MINIMAL, seen=seen)
        self._err(MINIMAL, "Duplicate watcher rule name 'eng'", seen=seen)

    def test_a_successful_parse_registers_the_name(self):
        seen: set[str] = set()
        parse(MINIMAL, seen=seen)
        self.assertEqual(seen, {"eng"})

    def test_room_cannot_be_combined_with_a_rooms_block(self):
        self._err({**MINIMAL, "room": "eng"}, "cannot be combined")

    def test_session_id_is_rejected_and_names_the_replacement(self):
        msg = self._err({**MINIMAL, "session_id": "abc"}, "no longer supported")
        self.assertIn("summarise the session to a file", msg)

    def test_unknown_key_inside_rooms(self):
        msg = self._err({**MINIMAL, "rooms": {"include": ["a"], "directt": True}}, "unknown key")
        self.assertIn("directt", msg)

    def test_rooms_must_be_a_mapping_when_parsed_directly(self):
        self._err({"name": "x", "rooms": ["a"]}, "'rooms' must be a mapping")

    def test_empty_include_with_no_dm_opt_in_can_never_match(self):
        self._err({"name": "x", "rooms": {}}, "can never match any room")

    def test_include_must_be_a_list_not_a_bare_string(self):
        self._err({**MINIMAL, "rooms": {"include": "eng-*"}}, "must be a list")

    def test_include_entries_must_be_non_empty_strings(self):
        for bad in ([""], [3], [None]):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "rooms": {"include": bad}}, "non-empty strings")

    def test_duplicate_pattern(self):
        self._err({**MINIMAL, "rooms": {"include": ["a", "a"]}}, "duplicate pattern")

    def test_invalid_pattern_is_reported_at_load_with_the_pattern_quoted(self):
        msg = self._err({**MINIMAL, "rooms": {"include": ["eng-[a"]}}, "is not valid")
        self.assertIn("eng-[a", msg)

    def test_exclude_without_include_is_a_no_op_and_refused(self):
        self._err(
            {"name": "x", "rooms": {"exclude": ["a"], "direct": True}},
            "no effect without",
        )

    def test_an_exclude_that_cannot_overlap_the_include_is_refused(self):
        """The footgun: `exclude: [ops-secret]` next to `include: [eng-*]` reads
        like protection and does nothing at all, because a name the include
        misses is NO_MATCH and falls through to the next rule."""
        msg = self._err(
            {**MINIMAL, "rooms": {"include": ["eng-*"], "exclude": ["ops-secret"]}},
            "can never match any room this rule includes",
        )
        # The message has to teach the fall-through fact, or the operator fixes
        # the pattern and still does not get what they wanted.
        self.assertIn("does not stop a *later* rule", msg)
        self.assertIn("include it here and exclude it", msg)

    def test_a_partially_overlapping_exclude_is_fine(self):
        r = parse({**MINIMAL, "rooms": {"include": ["eng-*"], "exclude": ["eng-archive"]}})
        self.assertIs(r.match("eng-backend", RoomKind.CHANNEL), RuleMatch.CLAIMED)
        self.assertIs(r.match("eng-archive", RoomKind.CHANNEL), RuleMatch.DECLINED)

    def test_only_the_offending_pattern_is_named(self):
        msg = self._err(
            {
                **MINIMAL,
                "rooms": {"include": ["eng-*"], "exclude": ["eng-old", "ops-x"]},
            },
            "'ops-x'",
        )
        self.assertNotIn("'eng-old'", msg)

    def test_dm_flags_reject_the_object_form_for_now(self):
        msg = self._err({**MINIMAL, "rooms": {"direct": {"include": ["*"]}}}, "does not yet")
        self.assertIn("planned extension", msg)

    def test_dm_flags_must_be_booleans(self):
        self._err({**MINIMAL, "rooms": {"direct": "yes"}}, "must be true or false")

    def test_unknown_connector(self):
        self._err({**MINIMAL, "connector": "nope"}, "unknown connector")

    def test_non_string_connector(self):
        self._err({**MINIMAL, "connector": ["mm-home"]}, "must be a string")

    def test_unknown_agent(self):
        self._err({**MINIMAL, "agent": "nope"}, "unknown agent")

    def test_ttl_must_be_a_positive_int(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "session_idle_days": bad}, "positive integer")

    def test_ttl_rejects_bool_which_is_an_int_subclass(self):
        self._err({**MINIMAL, "session_idle_days": True}, "positive integer")

    def test_idle_must_be_strictly_less_than_expire(self):
        self._err(
            {**MINIMAL, "session_idle_days": 30, "session_expire_days": 30},
            "strictly less than",
        )
        self._err(
            {**MINIMAL, "session_idle_days": 31, "session_expire_days": 30},
            "strictly less than",
        )

    def test_entry_must_be_a_mapping(self):
        self._err(["not", "a", "mapping"], "must be a mapping")


class TestTheDenyIdiom(unittest.TestCase):
    """Including a room and excluding it is how you blackhole it.

    It is the design's only way to express "no rule may claim this room": the
    rule claims the room, declines it, and DECLINED does not fall through. Worth
    pinning as intended behaviour rather than leaving it to look like a
    contradiction someone should "fix"."""

    def test_the_same_pattern_in_both_lists_is_accepted(self):
        r = parse({"name": "block", "rooms": {"include": ["eng-old"], "exclude": ["eng-old"]}})
        self.assertIs(r.match("eng-old", RoomKind.CHANNEL), RuleMatch.DECLINED)

    def test_it_blocks_later_rules_which_a_plain_omission_would_not(self):
        blocker = parse(
            {"name": "block", "rooms": {"include": ["eng-old"], "exclude": ["eng-old"]}},
            seen=set(),
        )
        catchall = parse({"name": "all", "rooms": {"include": ["eng-*"]}}, seen=set())

        def route(room: str) -> str:
            for rule_ in (blocker, catchall):
                outcome = rule_.match(room, RoomKind.CHANNEL)
                if outcome is not RuleMatch.NO_MATCH:
                    return f"{rule_.name}:{outcome.name}"
            return "unrouted"

        self.assertEqual(route("eng-old"), "block:DECLINED")
        self.assertEqual(route("eng-new"), "all:CLAIMED")

    def test_a_broader_deny_pattern_works_the_same_way(self):
        r = parse({"name": "block", "rooms": {"include": ["tmp-*"], "exclude": ["tmp-*"]}})
        self.assertIs(r.match("tmp-anything", RoomKind.CHANNEL), RuleMatch.DECLINED)


class TestShadowDetection(unittest.TestCase):
    def test_a_later_narrower_rule_is_shadowed(self):
        a = rule("broad", include=["eng-*"])
        b = rule("narrow", include=["eng-backend"])
        self.assertEqual(find_shadowed_rules([a, b]), [(b, a)])

    def test_order_matters(self):
        a = rule("broad", include=["eng-*"])
        b = rule("narrow", include=["eng-backend"])
        self.assertEqual(find_shadowed_rules([b, a]), [])

    def test_disjoint_rules_are_fine(self):
        self.assertEqual(
            find_shadowed_rules([rule("a", include=["eng-*"]), rule("b", include=["ops-*"])]),
            [],
        )

    def test_a_shadow_formed_only_by_a_union_is_not_reported(self):
        """A documented, deliberate gap rather than an oversight.

        `[ab]*` really is covered by `a*` together with `b*`, and the engine can
        prove it — `union_subsumes` takes a union on the outer side precisely so
        that it can. The check here compares against one earlier rule at a time
        anyway, matching §2.1's wording ("fully shadowed by an earlier one") and
        keeping the warning's attribution to a single named rule an operator can
        go and look at. Under-reporting is the chosen posture for this check, so
        the union case is silence rather than a vaguer message."""
        a = rule("a", include=["a*"])
        b = rule("b", include=["b*"])
        c = rule("c", include=["[ab]*"])
        self.assertEqual(find_shadowed_rules([a, b, c]), [])

    def test_but_a_single_earlier_rule_that_covers_it_is_reported(self):
        a = rule("a", include=["[ab]*"])
        c = rule("c", include=["a*"])
        self.assertEqual(find_shadowed_rules([a, c]), [(c, a)])

    def test_different_connectors_never_shadow(self):
        a = rule("a", connector="mm-home", include=["*"])
        b = rule("b", connector="rc-home", include=["eng-*"])
        self.assertEqual(find_shadowed_rules([a, b]), [])

    def test_a_rule_with_excludes_does_not_shadow(self):
        """The excluded slice is precisely what it does not claim."""
        a = rule("a", include=["eng-*"], exclude=["eng-archive"])
        b = rule("b", include=["eng-archive"])
        self.assertEqual(find_shadowed_rules([a, b]), [])

    def test_dm_opt_in_is_shadowed_only_by_an_earlier_dm_opt_in(self):
        broad = rule("broad", include=["*"])
        dms = rule("dms", direct=True)
        self.assertEqual(find_shadowed_rules([broad, dms]), [])

        earlier_dms = rule("earlier", direct=True)
        self.assertEqual(find_shadowed_rules([earlier_dms, dms]), [(dms, earlier_dms)])

    def test_group_dm_opt_in_is_tracked_separately(self):
        a = rule("a", direct=True)
        b = rule("b", direct=True, group_direct=True)
        self.assertEqual(find_shadowed_rules([a, b]), [])

    def test_a_rule_shadowed_despite_its_own_excludes(self):
        """Excludes only shrink the later rule, so it is still unreachable."""
        a = rule("a", include=["eng-*"])
        b = rule("b", include=["eng-*"], exclude=["eng-archive"])
        self.assertEqual(find_shadowed_rules([a, b]), [(b, a)])

    def test_star_shadows_everything_after_it(self):
        a = rule("a", include=["*"])
        b = rule("b", include=["eng-*"])
        c = rule("c", include=["ops-?"])
        found = find_shadowed_rules([a, b, c])
        self.assertEqual(found, [(b, a), (c, a)])

    def test_only_the_first_shadowing_rule_is_reported(self):
        a = rule("a", include=["*"])
        b = rule("b", include=["eng-*"])
        c = rule("c", include=["eng-backend"])
        found = find_shadowed_rules([a, b, c])
        self.assertEqual([(s.name, by.name) for s, by in found], [("b", "a"), ("c", "a")])

    def test_no_rules_no_findings(self):
        self.assertEqual(find_shadowed_rules([]), [])


if __name__ == "__main__":
    unittest.main()
