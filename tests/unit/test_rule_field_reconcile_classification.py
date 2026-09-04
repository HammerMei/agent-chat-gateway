"""Every `WatcherRule` field has a stated reconciliation consequence (#143).

Modelled on the state-field classification test: a field added to the rule
without a row in `MUTATIONS` fails here, locally, instead of quietly changing
what a restart does to existing watchers.
"""

import dataclasses
import unittest

from gateway.core.reconcile import reconcile_records
from gateway.core.room_pattern import RoomPattern
from gateway.core.watcher_rule import RoomMatcher, WatcherRule
from tests.helpers import ENG_ROOM as ROOM
from tests.helpers import make_record_from_rule, make_rule

# field -> (a change to that field alone, the action an existing record gets).
# Every field is in the frozen snapshot, so a change reaches the record; the
# only question is how. A field added to WatcherRule without a row here fails
# the classification test below.
MUTATIONS = {
    "name": (lambda r: dataclasses.replace(r, name="renamed"), "rematerialize"),
    # Scoping the rule to another connector leaves this connector's record
    # with no rule at all.
    "connector": (lambda r: dataclasses.replace(r, connector="other"), "expire"),
    "agent": (lambda r: dataclasses.replace(r, agent="b"), "rematerialize"),
    "rooms": (lambda r: dataclasses.replace(
        r, rooms=RoomMatcher(include=(RoomPattern("eng-*"),))), "rematerialize"),
    "session_idle_days": (lambda r: dataclasses.replace(
        r, session_idle_days=r.session_idle_days + 1), "rematerialize"),
    "session_expire_days": (lambda r: dataclasses.replace(
        r, session_expire_days=r.session_expire_days + 1), "rematerialize"),
    "context_inject_files": (lambda r: dataclasses.replace(
        r, context_inject_files=["notes.md"]), "rematerialize"),
    "history_handoff": (lambda r: dataclasses.replace(
        r, history_handoff=dataclasses.replace(
            r.history_handoff, enabled=not r.history_handoff.enabled)), "rematerialize"),
}


class TestEveryRuleFieldIsClassified(unittest.TestCase):

    def test_no_rule_field_is_unclassified(self):
        declared = {f.name for f in dataclasses.fields(WatcherRule)}
        self.assertEqual(declared - set(MUTATIONS), set(),
                         "a WatcherRule field has no reconciliation classification")
        self.assertEqual(set(MUTATIONS) - declared, set(), "stale entry")

    def test_each_field_change_has_its_stated_consequence(self):
        base = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(base, ROOM)
        for name, (mutate, expected) in MUTATIONS.items():
            with self.subTest(field=name):
                plan = reconcile_records([record], [mutate(base)], connector="default")
                self.assertEqual([a.action for a in plan.actions], [expected],
                                 f"changing {name} must reach the existing record as {expected}")

    def test_an_identical_rule_keeps_the_record(self):
        base = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(base, ROOM)
        plan = reconcile_records([record], [make_rule(room="eng-backend", name="eng", agent="a")],
                                 connector="default")
        self.assertEqual([a.action for a in plan.actions], ["keep"])
