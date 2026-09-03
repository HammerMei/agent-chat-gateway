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

HEADER = """\
connectors:
  - name: rc-first
    agent: default
    connector: rc-first
    type: rocketchat
    server: {url: http://localhost:3000, username: bot, password: pw}
  - name: mm-second
    agent: default
    connector: rc-first
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
    body = HEADER + extra + "watcher_rules:\n" + textwrap.indent(
        textwrap.dedent(watchers_block), "  "
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


ONE_RULE = """\
- name: eng-rooms
  agent: default
  connector: mm-second
  rooms:
    include: ["eng-*"]
"""


class TestRuleEntries(unittest.TestCase):
    def test_a_rule_entry_reaches_the_rule_parser(self):
        cfg = GatewayConfig.from_file(write_config(ONE_RULE))
        self.assertEqual([r.name for r in cfg.watcher_rules], ["eng-rooms"])
        self.assertEqual(cfg.watcher_rules[0].connector, "mm-second")

    def test_duplicate_rule_names_are_rejected(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config("""\
                - name: dup
                  agent: default
                  connector: rc-first
                  rooms: {include: ["a-*"]}
                - name: dup
                  agent: default
                  connector: rc-first
                  rooms: {include: ["b-*"]}
                """))
        self.assertIn("dup", str(cm.exception))

    def test_a_rule_inherits_from_watcher_templates(self):
        cfg = GatewayConfig.from_file(write_config(
            """\
            - name: eng
              connector: rc-first
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
                  agent: default
                  connector: rc-first
                  rooms: {include: ["a-*"], nonsense: true}
                """))
        self.assertIn("Watcher rule at index 0", str(cm.exception))

    def test_collect_config_attributes_the_rule_and_keeps_going(self):
        cfg, issues = collect_config(write_config("""\
            - name: fine
              agent: default
              connector: rc-first
              rooms: {include: ["ok-*"]}
            - name: broken
              agent: default
              connector: rc-first
              rooms: {include: ["a-*"], nonsense: true}
            - name: also-fine
              agent: default
              connector: rc-first
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].entity_kind, "watcher")
        self.assertEqual(issues[0].entity_name, "broken")
        # The entries either side of the broken one still parsed.
        self.assertEqual([r.name for r in cfg.watcher_rules], ["fine", "also-fine"])

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
            watcher_rules:
              - name: odd
                connector: rc-first
                agent: default
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
              agent: default
              connector: rc-first
              rooms: {include: ["a-*"], nonsense: true}
            - name: eng
              agent: default
              connector: rc-first
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(len(issues), 1, f"the second 'eng' was rejected too: {issues}")
        self.assertEqual([r.name for r in cfg.watcher_rules], ["eng"])


class TestShadowingIsReportedAsAWarning(unittest.TestCase):
    """Dead config, not broken config: the daemon starts fine, so the load must
    succeed and `validate_config()` must say so as a warning."""

    SHADOWED = """\
    - name: everything
      agent: default
      connector: mm-second
      rooms: {include: ["*"]}
    - name: never-fires
      agent: default
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
              agent: default
              connector: mm-second
              rooms: {direct: true}
            - name: eng-plus-dms
              agent: default
              connector: mm-second
              rooms: {include: ["eng-*"], direct: true}
            """))
        self.assertTrue(result.ok)
        warnings = [w for w in result.warnings if "eng-plus-dms" in w]
        self.assertEqual(len(warnings), 1, result.warnings)
        # Contract: names the DM kind in words a reader will recognise.
        self.assertIn("one-to-one direct message", warnings[0])

    def test_no_warning_when_rules_do_not_overlap(self):
        result = validate_config(write_config("""\
            - name: a
              agent: default
              connector: mm-second
              rooms: {include: ["a-*"]}
            - name: b
              agent: default
              connector: mm-second
              rooms: {include: ["b-*"]}
            """))
        self.assertEqual(result.warnings, [])

    def test_no_warning_across_different_connectors(self):
        """Two connectors are two room namespaces — an identical pattern on each is
        the normal multi-account setup, not a shadow."""
        result = validate_config(write_config("""\
            - name: on-rc
              agent: default
              connector: rc-first
              rooms: {include: ["*"]}
            - name: on-mm
              agent: default
              connector: mm-second
              rooms: {include: ["*"]}
            """))
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
