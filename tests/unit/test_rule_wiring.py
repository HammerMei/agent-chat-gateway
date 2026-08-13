"""The loader reaches the rule parser — the wiring, not the parsing.

Before this, `_parse_one_watcher_rule` and `find_shadowed_rules` existed and were
tested directly, but nothing in the loader called them: every hard error and every
warning in them was theoretical, and a rule-shaped `watchers:` entry hit the static
parser and was rejected. These tests cover the seam.

Two contracts matter more than the rest and are asserted first:

* An old-shape config must load **byte-identically**. Rules land beside the static
  watchers, not instead of them, until the watcher manager replaces the static path
  — deleting the old parser before then would leave the integration branch
  unrunnable.
* `collect_config()` must attribute a failed rule to one `ConfigIssue` and keep
  going. That is why every failure in either parser is a `ValueError` and never a
  `TypeError`: a TypeError escaping the loop aborts the whole validation pass and
  reports one global error instead of one bad entry among many good ones.

Run with:
    uv run python -m pytest tests/unit/test_rule_wiring.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import GatewayConfig, collect_config
from gateway.config_validate import validate_config
from gateway.configtool.model import EditableConfig

HEADER = """\
connectors:
  - name: rc-first
    type: rocketchat
    server: {url: http://localhost:3000, username: bot, password: pw}
  - name: mm-second
    type: mattermost
    server: {url: http://localhost:8065, token: t, team: lab}
agents:
  default:
    type: claude
    working_directory: /tmp
  ops:
    type: claude
    working_directory: /tmp
"""


def write_config(watchers_block: str, extra: str = "") -> str:
    body = HEADER + extra + "watchers:\n" + textwrap.indent(
        textwrap.dedent(watchers_block), "  "
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


STATIC_ONLY = """\
- room: general
- {room: eng, connector: mm-second, agent: ops}
- {rooms: [a, b], connector: mm-second}
"""

ONE_RULE = """\
- name: eng-rooms
  connector: mm-second
  rooms:
    include: ["eng-*"]
"""


class TestTheStaticPathIsUnchanged(unittest.TestCase):
    """The groundwork adds a path; it must not alter the one in production."""

    def test_a_static_only_config_loads_exactly_as_before(self):
        cfg = GatewayConfig.from_file(write_config(STATIC_ONLY))
        self.assertEqual(
            [(w.name, w.connector, w.room, w.agent) for w in cfg.watchers],
            [
                ("rc-first-general", "rc-first", "general", "default"),
                ("mm-second-eng", "mm-second", "eng", "ops"),
                ("mm-second-a", "mm-second", "a", "default"),
                ("mm-second-b", "mm-second", "b", "default"),
            ],
        )
        self.assertEqual(cfg.watcher_rules, [])

    def test_the_wildcard_room_rejection_still_stands(self):
        """`room: "*"` stays a hard error: the static path has no rule matching, and
        there is no room literally named "*". The rule shape is how you ask for
        pattern matching now."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- room: "*"\n'))
        self.assertIn("not implemented yet", str(cm.exception))

    def test_collect_config_agrees_with_from_file_on_a_static_config(self):
        cfg, issues = collect_config(write_config(STATIC_ONLY))
        self.assertEqual(issues, [])
        self.assertEqual(len(cfg.watchers), 4)
        self.assertEqual(cfg.watcher_rules, [])


class TestBothShapesInOneBlock(unittest.TestCase):
    def test_a_rule_entry_reaches_the_rule_parser(self):
        cfg = GatewayConfig.from_file(write_config(ONE_RULE))
        self.assertEqual(cfg.watchers, [])
        self.assertEqual([r.name for r in cfg.watcher_rules], ["eng-rooms"])
        self.assertEqual(cfg.watcher_rules[0].connector, "mm-second")

    def test_the_two_shapes_coexist_and_keep_document_order(self):
        cfg = GatewayConfig.from_file(write_config("""\
            - {room: first, connector: rc-first}
            - name: rule-a
              connector: mm-second
              rooms: {include: ["a-*"]}
            - {room: second, connector: rc-first}
            - name: rule-b
              connector: mm-second
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual([w.room for w in cfg.watchers], ["first", "second"])
        # Order matters for rules and nothing else: first-match precedence is
        # positional, so the list must never be keyed or sorted by name.
        self.assertEqual([r.name for r in cfg.watcher_rules], ["rule-a", "rule-b"])

    def test_a_rule_and_a_static_watcher_may_share_a_name(self):
        """Not a decision this PR makes — the two name sets are separate because they
        identify different things, and whether they must be globally unique is a
        materialization question for the uniqueness/manager work. Pinned so the
        current behaviour is visible if that changes."""
        cfg = GatewayConfig.from_file(write_config("""\
            - {name: shared, room: general, connector: rc-first}
            - name: shared
              connector: mm-second
              rooms: {include: ["x-*"]}
            """))
        self.assertEqual([w.name for w in cfg.watchers], ["shared"])
        self.assertEqual([r.name for r in cfg.watcher_rules], ["shared"])

    def test_duplicate_rule_names_are_rejected(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config("""\
                - name: dup
                  rooms: {include: ["a-*"]}
                - name: dup
                  rooms: {include: ["b-*"]}
                """))
        self.assertIn("dup", str(cm.exception))

    def test_a_rule_inherits_from_watcher_templates(self):
        cfg = GatewayConfig.from_file(write_config(
            """\
            - name: eng
              rooms: {include: ["eng-*"]}
              inherits: shared
            """,
            extra="watcher_templates:\n  shared:\n    agent: ops\n",
        ))
        self.assertEqual(cfg.watcher_rules[0].agent, "ops")


class TestRuleErrorsAreAttributedAndSurvivable(unittest.TestCase):
    """`collect_config()` reports every independent problem; from_file() stops at
    the first. Both must be true of rules, not just of static entries."""

    def test_from_file_fails_fast_on_a_bad_rule(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config("""\
                - name: bad
                  rooms: {include: ["a-*"], nonsense: true}
                """))
        self.assertIn("Watcher rule at index 0", str(cm.exception))

    def test_collect_config_attributes_the_rule_and_keeps_going(self):
        cfg, issues = collect_config(write_config("""\
            - {room: fine, connector: rc-first}
            - name: broken
              rooms: {include: ["a-*"], nonsense: true}
            - name: also-fine
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].entity_kind, "watcher")
        self.assertEqual(issues[0].entity_name, "broken")
        # The entries either side of the broken one still parsed.
        self.assertEqual([w.room for w in cfg.watchers], ["fine"])
        self.assertEqual([r.name for r in cfg.watcher_rules], ["also-fine"])

    def test_an_unnamed_broken_rule_is_attributed_by_index(self):
        _, issues = collect_config(write_config("""\
            - rooms: {include: ["a-*"]}
            """))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].entity_name, "(index 0)")

    def test_a_non_string_yaml_key_in_a_rule_stays_a_value_error(self):
        """The error path itself must not raise TypeError. `1: value` made the
        unknown-key formatter compare int with str; escaping this loop's `except
        ValueError` would abort the whole pass rather than flag one entry."""
        path = write_config("")
        Path(path).write_text(HEADER + textwrap.dedent("""\
            watchers:
              - name: odd
                rooms: {include: ["a-*"]}
                1: value
            """))
        _, issues = collect_config(path)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].entity_kind, "watcher")
        with self.assertRaises(ValueError):
            GatewayConfig.from_file(path)

    def test_a_failed_rule_does_not_reserve_its_name_for_a_later_one(self):
        """The rule parser stages its name for this reason; the loader must give it
        one `seen_rule_names` set for the whole loop so the staging is observable."""
        cfg, issues = collect_config(write_config("""\
            - name: eng
              rooms: {include: ["a-*"], nonsense: true}
            - name: eng
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(len(issues), 1, f"the second 'eng' was rejected too: {issues}")
        self.assertEqual([r.name for r in cfg.watcher_rules], ["eng"])


class TestShadowingIsReportedAsAWarning(unittest.TestCase):
    """Dead config, not broken config: the daemon starts fine, so the load must
    succeed and `validate_config()` must say so as a warning."""

    SHADOWED = """\
    - name: everything
      connector: mm-second
      rooms: {include: ["*"]}
    - name: never-fires
      connector: mm-second
      rooms: {include: ["eng-*"]}
    """

    def test_a_shadowed_rule_still_loads(self):
        cfg = GatewayConfig.from_file(write_config(self.SHADOWED))
        self.assertEqual(len(cfg.watcher_rules), 2)

    def test_validate_reports_it_as_a_warning_naming_both_rules(self):
        result = validate_config(write_config(self.SHADOWED))
        self.assertTrue(result.ok, f"shadowing must not be an error: {result.errors}")
        warnings = [w for w in result.warnings if "never-fires" in w]
        self.assertEqual(len(warnings), 1, result.warnings)
        self.assertIn("everything", warnings[0])
        findings = [
            f for f in result.findings
            if f.severity == "warning" and f.entity_name == "never-fires"
        ]
        self.assertEqual(len(findings), 1, result.findings)
        self.assertEqual(findings[0].entity_kind, "watcher")
        self.assertEqual(findings[0].field, "rooms")

    def test_a_partially_shadowed_rule_names_the_dead_reach(self):
        """A hybrid rule whose DM opt-in is dead looks healthy otherwise, which is
        why ShadowFinding carries a scope rather than being a pair."""
        result = validate_config(write_config("""\
            - name: dms
              connector: mm-second
              rooms: {direct: true}
            - name: eng-plus-dms
              connector: mm-second
              rooms: {include: ["eng-*"], direct: true}
            """))
        self.assertTrue(result.ok)
        warnings = [w for w in result.warnings if "eng-plus-dms" in w]
        self.assertEqual(len(warnings), 1, result.warnings)
        self.assertIn("1:1 DM", warnings[0])

    def test_no_warning_when_rules_do_not_overlap(self):
        result = validate_config(write_config("""\
            - name: a
              connector: mm-second
              rooms: {include: ["a-*"]}
            - name: b
              connector: mm-second
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(result.warnings, [])

    def test_no_warning_across_different_connectors(self):
        """Two connectors are two room namespaces — an identical pattern on each is
        the normal multi-account setup, not a shadow."""
        result = validate_config(write_config("""\
            - name: on-rc
              connector: rc-first
              rooms: {include: ["*"]}
            - name: on-mm
              connector: mm-second
              rooms: {include: ["*"]}
            """))
        self.assertEqual(result.warnings, [])


class TestTheConfigToolSkipsRulesKnowingly(unittest.TestCase):
    """A rule is not an expanded watcher and never becomes one in that table.

    It must be skipped by shape, not by letting the static parser reject it: post
    wiring a rule is *valid* config, so nothing reports an issue for it, and relying
    on the parser would leave a legal entry with no row and no explanation anywhere.
    """

    def test_only_the_static_entry_expands_and_nothing_is_reported(self):
        path = write_config("""\
            - {room: general, connector: rc-first}
            - name: eng-rooms
              connector: mm-second
              rooms: {include: ["eng-*"]}
            """)
        cfg = EditableConfig.load(path)
        expanded = cfg.expanded_watchers()
        self.assertEqual([e.watcher.room for e in expanded], ["general"])
        result = validate_config(path)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.warnings, [])

    def test_a_rules_only_config_yields_no_rows_rather_than_failing(self):
        cfg = EditableConfig.load(write_config(ONE_RULE))
        self.assertEqual(cfg.expanded_watchers(), [])


if __name__ == "__main__":
    unittest.main()
