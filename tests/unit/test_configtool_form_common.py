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

    def test_finds_a_watcher_that_only_inherits_its_connector_from_watcher_defaults(self):
        """watcher_defaults may set connector/agent (unlike name/room/rooms/
        session_id) — a watcher entry with no explicit 'connector:' still
        counts as referencing it."""
        cfg = self._cfg(f"""\
            watcher_defaults:
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
                agent: default
                room: general
        """)
        labels = find_referencing_watcher_labels(cfg, connector_name="rc")
        self.assertEqual(labels, ["my-watcher"])

    def test_label_falls_back_to_room_when_the_watcher_has_no_name(self):
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
        self.assertEqual(labels, ["general"])

    def test_label_falls_back_to_joined_rooms_when_the_watcher_has_no_name(self):
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
        self.assertEqual(labels, ["general, dev"])

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
