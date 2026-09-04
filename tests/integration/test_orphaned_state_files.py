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

        with self.assertLogs("agent-chat-gateway", level="INFO") as logs:
            GatewayService(write_gateway_config(self.tmp))  # the sweep runs in __init__

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
        with patch("pathlib.Path.unlink", side_effect=OSError("read-only")), \
                self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))

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

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))

        self.assertTrue((self.runtime / "state.ghost.json").exists(),
                        "deleting it would lose sess-ghost-bad without a trace")
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output))
        self.assertTrue(any("could not be parsed" in line for line in logs.output), logs.output)

    async def test_an_orphan_sharing_session_ids_with_a_live_file_does_not_block_the_boot(self):
        """Renaming a connector by copying its state file leaves the old file behind
        with the SAME session ids. The uniqueness preflight must see the fleet after
        the sweep, or the boot is refused for records about to be released."""
        from gateway.service import GatewayService

        record = {"watcher_name": "x:room", "session_id": "sess-shared-1", "room_id": "r1",
                  "connector": "x", "agent": "default", "rule_name": "w1", "rule": {"name": "w1"},
                  "config": {"name": "x:room", "connector": "x", "room": "room", "agent": "default"}}
        self._write_state("old-name", [record])
        self._write_state("script", [dict(record, watcher_name="script:room", connector="script")])

        GatewayService(write_gateway_config(self.tmp))  # no DuplicateSessionError

        self.assertFalse((self.runtime / "state.old-name.json").exists())
        self.assertTrue((self.runtime / "state.script.json").exists())

    async def test_a_file_whose_watchers_field_is_not_a_list_is_kept_and_does_not_block_the_boot(self):
        """`load_state` reads a null/scalar `watchers` as corrupt-but-loadable (empty);
        the sweep must not crash the constructor on it — one stale file must never
        keep every configured connector from starting."""
        from gateway.service import GatewayService

        (self.runtime / "state.ghost.json").write_text(json.dumps(
            {"version": STATE_FORMAT_VERSION, "watchers": None}))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))  # no TypeError

        self.assertTrue((self.runtime / "state.ghost.json").exists(), "kept for manual repair")
        self.assertTrue(any("could not be parsed" in line for line in logs.output), logs.output)
