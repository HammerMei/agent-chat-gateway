"""Unit tests for gateway/config.py's collect_config() — the fault-tolerant
counterpart to GatewayConfig.from_file() (which stays strict/fail-fast,
unchanged, for its existing production callers). These pin the specific
correctness properties an independent code review verified/caught while
this was built: partial progress is preserved across a structural failure
elsewhere, and a failed multi-room watcher entry never leaks a phantom
"duplicate name" collision onto a later, genuinely valid entry.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import _parse_one_watcher_entry, collect_config


class _CollectConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)


class TestCollectConfigWatcherNameLeak(_CollectConfigTestBase):
    """PR review finding: seen_watcher_names (shared across ALL watcher
    entries in one collect_config() pass) used to be updated AS EACH ROOM
    was processed, not just once the whole entry succeeded. A multi-room
    entry that registered its first room's name fine and then raised on a
    LATER room left that first room's name permanently staged as "seen" —
    even though the entry's failure means NONE of its watchers actually
    exist in the result — so a later, perfectly valid entry wanting that
    same name was rejected as a false "duplicate"."""

    def test_a_failed_multi_room_entry_does_not_poison_later_valid_entries(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: rc-random
                connector: rc
                room: "collision-room"
              - connector: rc
                rooms: ["general", "random"]
              - name: rc-general
                connector: rc
                room: "another-room"
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        names = [w.name for w in config.watchers]
        # entry 2 ("random" room auto-name collides with entry 0's explicit
        # name "rc-random"? No — entry 1's SECOND room "random" auto-names
        # to "rc-random" too, genuinely colliding with entry 0 — entry 1 as
        # a whole is correctly rejected. What must NOT happen: entry 2
        # ("rc-general") getting rejected as a phantom duplicate of
        # something entry 1 never actually contributed.
        self.assertIn("rc-general", names)
        watcher_issues = [i for i in issues if i.entity_kind == "watcher"]
        # Exactly the genuinely-broken entry (index 1) should be reported —
        # not entry 2.
        self.assertEqual(len(watcher_issues), 1)


class TestCollectConfigPartialProgressPreserved(_CollectConfigTestBase):
    """PR review finding: several structural-failure branches used to
    `return None, issues` outright, discarding every connector/agent that
    had ALREADY parsed successfully — silently hiding an unrelated,
    already-real problem (e.g. a connector's empty credentials) behind a
    completely different structural issue elsewhere in the file."""

    def test_invalid_default_agent_still_returns_the_good_connectors(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              broken_default:
                type: claude
              other_agent:
                type: claude
                working_directory: {self.agent_dir}
            default_agent: broken_default
            watchers:
              - connector: rc1
                agent: other_agent
                room: general
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(config.watchers, [])  # can't safely expand without a valid default_agent
        self.assertTrue(
            any("default_agent" in i.message and i.entity_kind == "global" for i in issues)
        )

    def test_all_connectors_failing_still_returns_the_good_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.connectors, [])
        self.assertEqual(list(config.agents), ["default"])

    def test_zero_agents_still_returns_the_good_connectors(self):
        config_path = self._write("""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: "http://localhost:3000", username: "", password: ""}
            agents:
              broken:
                type: claude
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(config.agents, {})

    def test_malformed_watchers_block_still_returns_good_connectors_and_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers: {{not: a-list}}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(list(config.agents), ["default"])
        self.assertEqual(config.watchers, [])


class TestParseOneWatcherEntryEmptyConnectors(_CollectConfigTestBase):
    """PR review finding: GatewayConfig.from_file() can never call
    _parse_one_watcher_entry() with an empty `connectors` list — an earlier
    structural check always raises first. collect_config() guards against
    it too (its own "no connectors parsed successfully" branch returns
    before ever reaching the watcher loop). But
    EditableConfig.expanded_watchers() calls this function directly, per
    raw watcher entry, against whatever partial `connectors` list
    collect_config() returned — so an all-connectors-failed config CAN
    legitimately reach this function with `connectors=[]`. Previously this
    crashed with an uncaught IndexError (`connectors[0].name`) instead of
    raising the ValueError every caller's `except ValueError` expects."""

    def test_no_explicit_connector_and_zero_connectors_raises_value_error_not_index_error(self):
        with self.assertRaises(ValueError):
            _parse_one_watcher_entry(
                {"name": "w1", "room": "general"},
                0,
                watcher_templates={},
                connector_names=set(),
                connectors=[],
                agents={},
                default_agent="",
                config_dir=Path(self.tmp),
                seen_watcher_names=set(),
            )


if __name__ == "__main__":
    unittest.main()
