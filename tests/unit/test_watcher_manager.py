"""Identity, labels and materialization for rule-derived watchers (design §2.3, §2.4).

Three things are derived from one room and must not be conflated: the **key**
`(connector, room_id)`, the cosmetic **label**, and the human-meaningful **room
description** that the agent is told it lives in. They coincide for a channel and diverge
for a group DM, which is where a conflation would first show up.
"""

import json
import unittest

from gateway.core.room_pattern import RoomPattern
from gateway.core.state import CONFIG_SCHEMA_VERSION
from gateway.core.watcher_manager import (
    RoomRef,
    first_matching_rule,
    materialize,
    room_description,
    room_label,
    rule_snapshot,
    snapshot_digest,
    watcher_label,
)
from gateway.core.watcher_rule import RoomKind, RoomMatcher, WatcherRule


def _rule(name="eng", connector="mm-eng", agent="claude", include=(), except_for=(),
          direct=False, group_direct=False, **kwargs):
    return WatcherRule(
        name=name,
        connector=connector,
        agent=agent,
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in include),
            except_for=tuple(RoomPattern(p) for p in except_for),
            direct=direct,
            group_direct=group_direct,
        ),
        **kwargs,
    )


class TestLabels(unittest.TestCase):
    def test_a_channel_is_labelled_by_its_name(self):
        room = RoomRef(id="r1o6c8", kind=RoomKind.CHANNEL, name="incident-42")
        self.assertEqual(watcher_label("mm-eng", room), "mm-eng-incident-42")

    def test_a_one_to_one_dm_is_labelled_by_its_counterpart(self):
        room = RoomRef(id="iwihkh", kind=RoomKind.DM, participants=("alice",))
        self.assertEqual(watcher_label("mm-eng", room), "mm-eng-dm-alice")

    def test_a_group_dm_is_labelled_by_a_digest_of_its_room_id(self):
        """Deliberately not by its members.

        The tempting alternative is Mattermost's `channel_display_name`, which *is* the
        member list — but it moves whenever membership does, includes the bot itself, has
        no documented ordering, and Rocket.Chat has no equivalent, so one kind of room
        would be labelled by different rules on the two platforms.
        """
        room = RoomRef(id="cib3hj", kind=RoomKind.GROUP_DM, participants=("@bob", "@alice"))
        label = watcher_label("mm-eng", room)
        self.assertTrue(label.startswith("mm-eng-gdm-"), label)
        self.assertNotIn("alice", label)

        # And it does not move when the members do.
        reordered = RoomRef(id="cib3hj", kind=RoomKind.GROUP_DM,
                            participants=("@alice", "@bob", "@carol"))
        self.assertEqual(watcher_label("mm-eng", reordered), label)

    def test_a_group_dm_label_is_stable_and_per_room(self):
        a = RoomRef(id="room-a", kind=RoomKind.GROUP_DM, participants=("@x",))
        b = RoomRef(id="room-b", kind=RoomKind.GROUP_DM, participants=("@x",))
        self.assertEqual(room_label(a), room_label(a))
        self.assertNotEqual(room_label(a), room_label(b))

    def test_a_nameless_channel_still_gets_a_label(self):
        """A label is cosmetic, so refusing to name a room would be a worse failure than
        naming it dully."""
        room = RoomRef(id="r9", kind=RoomKind.CHANNEL, name="")
        self.assertTrue(room_label(room))


class TestRoomDescription(unittest.TestCase):
    """`WatcherConfig.room` for a materialized watcher — what the agent is *told*.

    This is the field the durable identity header renders on every turn, which is why it
    must never hold a pattern: a rule-shaped value would permanently tell an agent its
    room is `eng-*`.
    """

    def test_a_channel_describes_itself_by_name(self):
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="incident-42")
        self.assertEqual(room_description(room), "incident-42")

    def test_a_group_dm_describes_itself_by_its_members(self):
        """Where label and description deliberately diverge (§2.4): the label is a digest,
        but "where do you live" is answered by who is in the room."""
        room = RoomRef(id="cib3hj", kind=RoomKind.GROUP_DM,
                       participants=("@alice", "@bob"))
        self.assertEqual(room_description(room), "@alice, @bob")
        self.assertNotEqual(room_description(room), room_label(room))

    def test_a_one_to_one_dm_describes_itself_by_the_counterpart(self):
        room = RoomRef(id="iwih", kind=RoomKind.DM, participants=("alice",))
        self.assertEqual(room_description(room), "alice")


class TestMaterialization(unittest.TestCase):
    def test_the_two_overwritten_fields(self):
        rule = _rule(name="eng-rooms", include=["eng-*"])
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend")

        wc = materialize(rule, room)

        self.assertEqual(wc.name, "mm-eng-eng-backend", "the rule's name is not a watcher's")
        self.assertEqual(wc.room, "eng-backend")
        self.assertEqual(wc.connector, "mm-eng")
        self.assertEqual(wc.agent, "claude")

    def test_the_materialized_room_never_holds_a_pattern(self):
        """The assertion §2.4 asks for by name. The identity header re-supplies this value
        every turn, so a pattern here is not a cosmetic problem — it is a permanent lie to
        the agent about where it is."""
        rule = _rule(include=["eng-*", "incident-?", "[ab]-team"])
        for room in (
            RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend"),
            RoomRef(id="r2", kind=RoomKind.GROUP, name="eng-private"),
            RoomRef(id="r3", kind=RoomKind.DM, participants=("alice",)),
            RoomRef(id="r4", kind=RoomKind.GROUP_DM, participants=("@a", "@b")),
        ):
            with self.subTest(kind=room.kind.value):
                materialized = materialize(rule, room).room
                for metacharacter in "*?[]":
                    self.assertNotIn(metacharacter, materialized)

    def test_the_rest_of_the_rule_is_carried_across(self):
        from gateway.core.config import HistoryHandoffConfig

        rule = _rule(
            include=["eng-*"],
            context_inject_files=["/srv/notes.md"],
            online_notification="up",
            offline_notification="down",
            history_handoff=HistoryHandoffConfig(enabled=True, fetch_count=25),
        )
        wc = materialize(rule, RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-x"))

        self.assertEqual(wc.context_inject_files, ["/srv/notes.md"])
        self.assertEqual(wc.online_notification, "up")
        self.assertEqual(wc.offline_notification, "down")
        self.assertTrue(wc.history_handoff.enabled)
        self.assertEqual(wc.history_handoff.fetch_count, 25)

    def test_the_carried_collections_are_copies(self):
        """Every watcher a rule creates would otherwise share one list, so appending to one
        watcher's context files would append to every sibling's."""
        rule = _rule(include=["eng-*"], context_inject_files=["/a"])
        wc = materialize(rule, RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-x"))

        wc.context_inject_files.append("/b")
        wc.history_handoff.enabled = not rule.history_handoff.enabled

        self.assertEqual(rule.context_inject_files, ["/a"])
        self.assertNotEqual(wc.history_handoff.enabled, rule.history_handoff.enabled)


class TestRuleSnapshot(unittest.TestCase):
    """The drift baseline, which has to survive a JSON round trip to be stored at all."""

    def test_it_is_json_serializable(self):
        """`dataclasses.asdict` is not enough: a rule's patterns are `RoomPattern` objects
        with `__slots__`, so `asdict` leaves them as objects and the first save raises."""
        snapshot = rule_snapshot(_rule(include=["eng-*"], except_for=["eng-archive"]))
        restored = json.loads(json.dumps(snapshot))
        self.assertEqual(restored["rooms"]["include"], ["eng-*"])
        self.assertEqual(restored["rooms"]["except_for"], ["eng-archive"])

    def test_patterns_are_stored_as_written(self):
        """Also the only form an operator can compare against their own config.yaml."""
        snapshot = rule_snapshot(_rule(include=["eng-*", "incident-?"]))
        self.assertEqual(snapshot["rooms"]["include"], ["eng-*", "incident-?"])

    def test_the_dm_opt_ins_are_part_of_the_baseline(self):
        snapshot = rule_snapshot(_rule(direct=True, group_direct=False))
        self.assertTrue(snapshot["rooms"]["direct"])
        self.assertFalse(snapshot["rooms"]["group_direct"])

    def test_the_digest_changes_with_any_field(self):
        base = rule_snapshot(_rule(include=["eng-*"]))
        for label, other in {
            "include": _rule(include=["eng-*", "ops-*"]),
            "except_for": _rule(include=["eng-*"], except_for=["eng-archive"]),
            "agent": _rule(include=["eng-*"], agent="claude-2"),
            "direct": _rule(include=["eng-*"], direct=True),
            "idle_days": _rule(include=["eng-*"], session_idle_days=7),
        }.items():
            with self.subTest(changed=label):
                self.assertNotEqual(snapshot_digest(base),
                                    snapshot_digest(rule_snapshot(other)))

    def test_the_digest_ignores_how_the_dict_was_built(self):
        """Sorted keys, so the digest depends on content rather than insertion order —
        otherwise an unrelated refactor of `rule_snapshot` would report every watcher as
        drifted."""
        snapshot = rule_snapshot(_rule(include=["eng-*"]))
        shuffled = dict(reversed(list(snapshot.items())))
        self.assertEqual(snapshot_digest(snapshot), snapshot_digest(shuffled))

    def test_the_materialized_config_could_not_serve_as_this_baseline(self):
        """§2.4's reason for storing both: the materialized config's `name` and `room` are
        overwritten by construction, so diffing it against a rule would report those two
        as changed on every comparison."""
        rule = _rule(name="eng-rooms", include=["eng-*"])
        wc = materialize(rule, RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend"))
        self.assertNotEqual(wc.name, rule.name)
        self.assertNotEqual(wc.room, rule.rooms.include[0].raw)


class TestFirstMatchingRule(unittest.TestCase):
    """First match in config order wins, and a decline halts the search (§2.2)."""

    def test_the_first_claim_wins(self):
        rules = [_rule(name="a", include=["eng-*"]), _rule(name="b", include=["eng-*"])]
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend")
        self.assertEqual(first_matching_rule(rules, "mm-eng", room).name, "a")

    def test_a_decline_halts_rather_than_falling_through(self):
        """`except_for` produces DECLINED, which stops routing — a deny rule shadows later
        rules for that room completely, which is its purpose. Returning the first *claim*
        while letting a decline fall through would quietly invert that."""
        rules = [
            _rule(name="deny", include=["eng-archive"], except_for=["eng-archive"]),
            _rule(name="catch-all", include=["eng-*"]),
        ]
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-archive")
        self.assertIsNone(first_matching_rule(rules, "mm-eng", room))

    def test_rules_for_other_connectors_are_skipped_not_matched(self):
        rules = [
            _rule(name="other", connector="mm-sales", include=["eng-*"]),
            _rule(name="mine", include=["eng-*"]),
        ]
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend")
        self.assertEqual(first_matching_rule(rules, "mm-eng", room).name, "mine")

    def test_no_rule_claiming_the_room_returns_none(self):
        rules = [_rule(include=["ops-*"])]
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend")
        self.assertIsNone(first_matching_rule(rules, "mm-eng", room))

    def test_a_dm_matches_only_a_rule_that_opted_in(self):
        room = RoomRef(id="d1", kind=RoomKind.DM, participants=("alice",))
        self.assertIsNone(first_matching_rule([_rule(include=["*"])], "mm-eng", room))
        self.assertIsNotNone(
            first_matching_rule([_rule(direct=True)], "mm-eng", room))

    def test_a_group_dm_matches_only_a_group_opt_in(self):
        """The two DM kinds are separate classes: a rule taking 1:1 DMs does not take
        group DMs."""
        room = RoomRef(id="g1", kind=RoomKind.GROUP_DM, participants=("@a", "@b"))
        self.assertIsNone(first_matching_rule([_rule(direct=True)], "mm-eng", room))
        self.assertIsNotNone(
            first_matching_rule([_rule(group_direct=True)], "mm-eng", room))

    def test_a_pattern_cannot_claim_a_dm_kind_at_all(self):
        """`RoomMatcher.match()` short-circuits on kind before consulting any pattern, so
        the name it is handed for a DM is irrelevant — including the empty string both DM
        kinds carry. Asserted because the first version of `first_matching_rule` passed the
        *label* for group DMs to stop a pattern matching a digest, which the matcher already
        makes impossible: a branch guarding a case that cannot arise.
        """
        for kind in (RoomKind.DM, RoomKind.GROUP_DM):
            with self.subTest(kind=kind.value):
                room = RoomRef(id="gdm-lookalike", kind=kind, participants=("@a",))
                self.assertIsNone(
                    first_matching_rule([_rule(include=["gdm-*", "*"])], "mm-eng", room))


class TestConfigSchemaVersion(unittest.TestCase):
    def test_it_is_a_separate_number_from_the_state_format(self):
        """Two versions in one record, answering different questions: the file-level one is
        "can this build read these records", enforced by refusing to start; this one is
        "what did this snapshot's fields mean when written", which must *not* refuse a
        readable file."""
        from gateway.core.state import STATE_FORMAT_VERSION

        self.assertIsInstance(CONFIG_SCHEMA_VERSION, int)
        self.assertGreater(CONFIG_SCHEMA_VERSION, 0)
        # Same value today by coincidence would be fine; what matters is that they are
        # separate names, so this asserts the names exist rather than a relation.
        self.assertIsInstance(STATE_FORMAT_VERSION, int)

    def test_a_record_defaults_to_no_snapshot(self):
        """0 means "no snapshot" — a record written by the static path, which carries no
        rule at all. Not version 1, which would claim a snapshot exists."""
        from gateway.core.state import WatcherState

        self.assertEqual(WatcherState(watcher_name="w", session_id="", room_id="")
                         .config_schema_version, 0)


if __name__ == "__main__":
    unittest.main()
