"""A state file for a connector that is no longer configured is reclaimed at
boot: each record's session id is logged, then the file is removed (#143).

Seam: a real `GatewayService` built from a config file.
"""

import json
import unittest

from gateway.core.state import STATE_FORMAT_VERSION
from tests.helpers import isolate_runtime_dir, write_gateway_config


class TestOrphanedStateFiles(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)

    def _write_state(self, connector, records):
        (self.runtime / f"state.{connector}.json").write_text(json.dumps(
            {"version": STATE_FORMAT_VERSION, "watchers": records}))

    async def test_records_of_an_unconfigured_connector_are_logged_and_the_file_removed(self):
        from gateway.service import GatewayService

        self._write_state("ghost", [{
            "watcher_name": "ghost:old-room", "session_id": "sess-ghost-7777",
            "room_id": "r-old", "connector": "ghost", "agent": "default",
            "rule_name": "eng", "rule": {"name": "eng"},
            "config": {"name": "ghost:old-room", "connector": "ghost",
                       "room": "old-room", "agent": "default"},
        }])
        self._write_state("script", [])
        service = GatewayService(write_gateway_config(self.tmp))

        with self.assertLogs("agent-chat-gateway", level="INFO") as logs:
            service._reclaim_orphaned_state_files()

        self.assertFalse((self.runtime / "state.ghost.json").exists(),
                         "nothing will ever open it again")
        self.assertTrue((self.runtime / "state.script.json").exists(),
                        "the configured connector's file is not touched")
        audit = [line for line in logs.output
                 if "AUDIT: session released" in line and "sess-ghost-7777" in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn("connector-removed", audit[0])

    async def test_a_file_that_cannot_be_removed_is_not_reported_released(self):
        from unittest.mock import patch

        from gateway.service import GatewayService

        self._write_state("ghost", [{
            "watcher_name": "ghost:old-room", "session_id": "sess-ghost-8888",
            "room_id": "r-old", "connector": "ghost", "agent": "default",
            "rule_name": "eng", "rule": {"name": "eng"},
            "config": {"name": "ghost:old-room", "connector": "ghost",
                       "room": "old-room", "agent": "default"},
        }])
        service = GatewayService(write_gateway_config(self.tmp))

        with patch("pathlib.Path.unlink", side_effect=OSError("read-only")), \
                self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            service._reclaim_orphaned_state_files()

        self.assertTrue((self.runtime / "state.ghost.json").exists())
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output),
                         "a release that did not happen is not announced")
        self.assertTrue(any("remain until the next start" in line for line in logs.output))

    async def test_a_file_with_a_record_that_does_not_parse_is_kept(self):
        from gateway.service import GatewayService

        good = {"watcher_name": "ghost:ok", "session_id": "sess-ghost-ok",
                "room_id": "r-ok", "connector": "ghost", "agent": "default",
                "rule_name": "eng", "rule": {"name": "eng"},
                "config": {"name": "ghost:ok", "connector": "ghost", "room": "ok", "agent": "default"}}
        malformed = dict(good, watcher_name="ghost:bad", session_id="sess-ghost-bad",
                         room_id="r-bad", paused="yes please")  # not a bool: load_state skips it
        self._write_state("ghost", [good, malformed])
        service = GatewayService(write_gateway_config(self.tmp))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            service._reclaim_orphaned_state_files()

        self.assertTrue((self.runtime / "state.ghost.json").exists(),
                        "deleting it would lose sess-ghost-bad without a trace")
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output))
        self.assertTrue(any("could not be parsed" in line for line in logs.output), logs.output)
