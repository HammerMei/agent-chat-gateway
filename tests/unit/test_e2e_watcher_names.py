"""The E2E schedule test hardcodes watcher HANDLES; pin the format here.

`tests/e2e/test_schedule_e2e.py` builds `<connector>:<channel>` and
`<connector>:dm:<user>` by hand, because `schedule create` resolves its
argument against persisted watcher records rather than against config. That
is a real coupling to `watcher_label()`'s output format, and the E2E suite
cannot check it: it needs a live Rocket.Chat, so a rename of the labelling
scheme would sit undetected until someone ran the lab — which is exactly how
that file came to be passing RULE names after the dynamic-watcher cutover
(caught by hand, not by a test).

So the format assumption is asserted here, where it runs on every commit.
If this fails, `tests/e2e/test_schedule_e2e.py` needs the same change.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from gateway.core.watcher_manager import RoomRef, watcher_label
from gateway.core.watcher_rule import RoomKind

_REPO = Path(__file__).resolve().parents[2]
_E2E = _REPO / "tests" / "e2e"
_E2E_CONFIG = _E2E / "acg-config" / "config.yaml"
_SCHEDULE_TEST = _E2E / "test_schedule_e2e.py"


class TestE2EWatcherHandleFormat(unittest.TestCase):
    def test_a_channel_handle_is_connector_colon_channel_name(self):
        self.assertEqual(
            "rc-e2e:acg-e2e-claude",
            watcher_label(
                "rc-e2e",
                RoomRef(
                    id="whatever",
                    kind=RoomKind.CHANNEL,
                    name="acg-e2e-claude",
                    participants=(),
                ),
            ),
        )

    def test_a_dm_handle_is_connector_colon_dm_colon_counterpart(self):
        """The counterpart is the human, never the bot — which is why the
        E2E fixture interpolates the test user and not `acg_bot`."""
        self.assertEqual(
            "rc-e2e:dm:test_user",
            watcher_label(
                "rc-e2e",
                RoomRef(
                    id="whatever",
                    kind=RoomKind.DM,
                    name="",
                    participants=("test_user", "acg_bot"),
                ),
            ),
        )


class TestE2EScheduleTestTargetsWatchersNotRules(unittest.TestCase):
    """The mistake this guards against is specific: passing a RULE name to
    `schedule create`, which fails with "Watcher ... not found in any
    connector" because rules name no room and so have no handle."""

    def setUp(self):
        self.source = _SCHEDULE_TEST.read_text()
        self.rule_names = {
            entry["name"]
            for entry in (yaml.safe_load(_E2E_CONFIG.read_text()).get("watchers") or [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }

    def test_the_e2e_config_still_defines_the_rules_this_pins_against(self):
        """A guard on the guard: if the config stopped defining these, the
        assertions below would pass by vacuity."""
        self.assertIn("e2e-claude-channel", self.rule_names)
        self.assertIn("e2e-dm", self.rule_names)

    def test_no_rule_name_is_passed_as_a_watcher(self):
        assigned = set(re.findall(r'"watcher":\s*(.+?),\s*$', self.source, re.M))
        self.assertTrue(assigned, "no 'watcher' values found — did the fixture move?")
        for value in assigned:
            for rule in self.rule_names:
                self.assertNotIn(
                    f'"{rule}"',
                    value,
                    f"{_SCHEDULE_TEST.name} passes the RULE name {rule!r} where a "
                    "watcher handle is required — schedule create resolves against "
                    "persisted watcher records, so this fails at runtime with "
                    '"Watcher ... not found in any connector".',
                )

    def test_the_connector_half_matches_the_e2e_config(self):
        """The hardcoded connector name must be the one the config declares,
        or every handle it builds is wrong."""
        declared = {
            c["name"]
            for c in (yaml.safe_load(_E2E_CONFIG.read_text()).get("connectors") or [])
            if isinstance(c, dict) and isinstance(c.get("name"), str)
        }
        match = re.search(r'^CONNECTOR_NAME\s*=\s*"([^"]+)"', self.source, re.M)
        self.assertIsNotNone(match, "CONNECTOR_NAME not found in the schedule E2E test")
        self.assertIn(match.group(1), declared)


if __name__ == "__main__":
    unittest.main()
