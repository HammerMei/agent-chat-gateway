"""Unit tests for gateway/configtool/screens/form_common.py's standalone
helpers — find_referencing_watcher_labels() specifically, since it's the
basis for the pre-delete "still used by watcher(s): ..." check on both
AgentDetailScreen and ConnectorDetailScreen.

Rewritten with the Rules tab: a `watchers:` entry is a RULE with a required
unique name, so labels are the rules' own names (position fallback for a
malformed nameless entry) and matching walks the raw entries' MERGED view —
it no longer needs the whole config to load, which the old
expanded-watchers implementation did. That old implementation returned []
for every rule (rules never expanded), silently unblocking the deletion of
a connector every rule referenced — the regression this suite now pins
against.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.configtool.model import EditableConfig
from gateway.configtool.screens.form_common import find_referencing_watcher_labels


class TestFindReferencingWatcherLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agent_dir = self.tmp / "work"
        self.agent_dir.mkdir()

    def _cfg(self, yaml_text: str) -> EditableConfig:
        path = self.tmp / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return EditableConfig.load(path)

    def _base(self, watchers_yaml: str, extra_top: str = "") -> EditableConfig:
        return self._cfg(f"""\
            {extra_top}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
{watchers_yaml}
        """)

    def test_finds_a_rule_by_explicit_connector(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_finds_a_rule_by_explicit_agent(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="default"), ["my-rule"])

    def test_returns_empty_when_nothing_references_the_name(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="unrelated"), [])
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="unrelated"), [])

    def test_finds_a_rule_that_only_inherits_its_connector_from_a_template(self):
        """A watcher_templates: entry may set connector/agent (unlike
        name/room/rooms/session_id) — a rule with no explicit 'connector:'
        of its own, only inheriting one via 'inherits:', still counts as
        referencing it (checked against the MERGED view)."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                inherits: standard\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n",
            extra_top="watcher_templates:\n              standard:\n                connector: rc\n",
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_a_nameless_malformed_entry_falls_back_to_its_position_label(self):
        """A rule's name is required — a raw entry without one is malformed
        (the loader refuses it), but if it names the connector, deleting
        that connector still deserves a block with SOME label."""
        cfg = self._base(
            "              - connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["watchers[0]"])

    def test_a_broken_config_still_blocks_on_explicit_references(self):
        """Regression: the old expanded-watchers implementation returned []
        whenever the config didn't fully load, so deleting a connector that
        a (broken) rule explicitly referenced went unblocked. Matching is
        raw-entry-based now — an unrelated breakage elsewhere must not
        silently unblock this deletion."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: nonexistent-agent\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_a_rule_relying_on_the_loader_fallback_is_not_matched(self):
        """No connector anywhere on the rule (loader falls back to the
        single connector): deliberately NOT matched — which entity the
        fallback resolves to shifts with the config itself; save()'s own
        validation remains the backstop for a deletion that breaks it."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), [])

    def test_multiple_referencing_rules_are_all_returned(self):
        cfg = self._base(
            "              - name: rule-a\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
            "              - name: rule-b\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [dev]\n"
        )
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc"), ["rule-a", "rule-b"]
        )

    def test_both_connector_and_agent_filters_must_match(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
              other:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: rule-a
                connector: rc
                agent: other
                rooms:
                  include: [general]
        """)
        # connector matches but agent doesn't -> no match
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="default"), []
        )
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="other"),
            ["rule-a"],
        )


if __name__ == "__main__":
    unittest.main()
