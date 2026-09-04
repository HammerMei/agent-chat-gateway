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
