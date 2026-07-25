"""Unit tests for gateway/configtool/screens/form_common.py's standalone
helpers — find_referencing_watcher_labels() specifically, since it's the
basis for the pre-delete "still used by watcher(s): ..." check on both
AgentDetailScreen and ConnectorDetailScreen.
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

    def test_finds_a_watcher_by_explicit_connector(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: my-watcher
                connector: rc
                agent: default
                room: general
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["my-watcher"])

    def test_finds_a_watcher_by_explicit_agent(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: my-watcher
                connector: rc
                agent: default
                room: general
        """)
        labels = find_referencing_watcher_labels(cfg, agent_name="default")
        self.assertEqual(labels, ["my-watcher"])

    def test_returns_empty_when_nothing_references_the_name(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: my-watcher
                connector: rc
                agent: default
                room: general
        """)
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="unrelated"), [])
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="unrelated"), [])

    def test_finds_a_watcher_that_only_inherits_its_connector_from_a_template(self):
        """A watcher_templates: entry may set connector/agent (unlike
        name/room/rooms/session_id) — a watcher entry with no explicit
        'connector:' of its own, only inheriting one via 'inherits:', still
        counts as referencing it. Goes through the real loader
        (expanded_watchers() -> GatewayConfig.from_file()), which resolves
        inherits: templates just like it resolves anything else — this is
        NOT one of the TUI's own stale *_defaults-display concerns."""
        cfg = self._cfg(f"""\
            watcher_templates:
              standard:
                connector: rc
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: my-watcher
                inherits: standard
                agent: default
                room: general
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["my-watcher"])

    def test_label_uses_the_real_auto_generated_name_when_the_watcher_has_no_name(self):
        """The real name an unnamed watcher gets everywhere else in the TUI
        (e.g. the Overview's Watchers tab) is `_auto_watcher_name()`'s
        "<connector>-<room>" (gateway/config.py) — NOT the bare room string.
        A user-reported mismatch here was the actual bug this test pins."""
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - connector: rc
                agent: default
                room: general
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["rc-general"])

    def test_a_rooms_group_produces_one_label_per_real_expanded_watcher(self):
        """A `rooms: [a, b]` entry is 2 SEPARATE real watchers (rc-general,
        rc-dev), not one joined "general, dev" string."""
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - connector: rc
                agent: default
                rooms: [general, dev]
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["rc-general", "rc-dev"])

    def test_returns_empty_when_the_config_does_not_currently_load(self):
        """A delete pre-check has nothing useful to say if the config is
        already broken for some unrelated reason — save()'s own validation
        remains the backstop for that; this must not raise."""
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - connector: rc
                agent: nonexistent-agent
                room: general
        """)
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), [])

    def test_multiple_referencing_watchers_are_all_returned(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: watcher-a
                connector: rc
                agent: default
                room: general
              - name: watcher-b
                connector: rc
                agent: default
                room: dev
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["watcher-a", "watcher-b"])

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
              - name: watcher-a
                connector: rc
                agent: other
                room: general
        """)
        # connector matches but agent doesn't -> no match
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="default"), []
        )
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="other"),
            ["watcher-a"],
        )


if __name__ == "__main__":
    unittest.main()
