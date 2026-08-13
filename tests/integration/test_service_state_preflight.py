"""The daemon refuses to start on a state file it cannot read.

`load_state`'s version check only matters if something opens the file. Every path that
would otherwise notice one is **per-connector**: `GatewayService` builds a session
manager for each *configured* connector, and `config validate`'s orphan check iterated
the same list. So a state file whose connector was renamed or removed in `config.yaml`
was never opened — and the daemon would start successfully while abandoning every
session in it. That is the outcome the refusal exists to prevent, reached by a
different route, which is why the preflight enumerates files on disk instead.

No test constructed `GatewayService` before this one, which is a large part of why the
gap was invisible: the refusal was covered at the `load_state` level and nothing
checked that the daemon consults it.

Run with:
    uv run python -m pytest tests/integration/test_service_state_preflight.py -v
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig
from gateway.core import state as state_mod
from gateway.core.state import (
    STATE_FORMAT_VERSION,
    FutureStateError,
    LegacyStateError,
    WatcherState,
    save_state,
)
from gateway.service import GatewayService

pytestmark = pytest.mark.integration


class TestServiceStatePreflight(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        patcher = patch.object(state_mod, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, connector_name: str = "script") -> GatewayConfig:
        """A script connector: constructible with no network or subprocess."""
        path = self.tmp / "config.yaml"
        path.write_text(textwrap.dedent(f"""\
            connectors:
              - name: {connector_name}
                type: script
            agents:
              default:
                type: claude
                working_directory: {self.tmp}
            watchers:
              - name: w1
                connector: {connector_name}
                room: script
        """))
        return GatewayConfig.from_file(str(path))

    def _write_unversioned(self, connector_name: str) -> Path:
        path = self.runtime / f"state.{connector_name}.json"
        path.write_text(json.dumps({
            "watchers": [{"watcher_name": "w1", "session_id": "s", "room_id": "r"}]
        }))
        return path

    def test_a_legacy_file_stops_the_daemon_from_starting(self):
        path = self._write_unversioned("script")
        with self.assertRaises(LegacyStateError) as cm:
            GatewayService(self._config())
        self.assertIn(str(path), str(cm.exception))

    def test_a_file_for_a_connector_no_longer_in_config_also_stops_it(self):
        """The case the per-connector paths could never see. `config.yaml` names only
        `script`, so nothing would ever have opened `state.old-rc.json` — and its
        sessions would have been abandoned by a boot that reported success."""
        path = self._write_unversioned("old-rc")
        save_state("script", [])  # the configured connector is fine
        with self.assertRaises(LegacyStateError) as cm:
            GatewayService(self._config())
        self.assertIn("state.old-rc.json", str(cm.exception))
        self.assertIn(str(path), str(cm.exception))

    def test_a_future_file_stops_it_with_the_non_destructive_message(self):
        (self.runtime / "state.script.json").write_text(
            json.dumps({"version": STATE_FORMAT_VERSION + 1, "watchers": []})
        )
        with self.assertRaises(FutureStateError) as cm:
            GatewayService(self._config())
        self.assertIn("Do not delete", str(cm.exception))

    def test_current_files_let_it_start(self):
        save_state("script", [
            WatcherState(watcher_name="w1", session_id="s", room_id="r")
        ])
        service = GatewayService(self._config())
        self.assertEqual([e.name for e in service._entries], ["script"])

    def test_no_state_files_at_all_let_it_start(self):
        service = GatewayService(self._config())
        self.assertEqual([e.name for e in service._entries], ["script"])

    def test_a_corrupt_file_does_not_stop_it(self):
        """Corruption keeps its graceful path: the file holds no recoverable state
        either way, so refusing to boot over it would trade a degradation for an
        outage. Only a readable file in an unreadable format is worth refusing."""
        (self.runtime / "state.script.json").write_text("{ not json")
        service = GatewayService(self._config())
        self.assertEqual([e.name for e in service._entries], ["script"])

    def test_the_preflight_runs_before_anything_is_built(self):
        """Ordering matters: connectors and agent backends are constructed in the same
        __init__, and a refusal after that would leave half-built objects behind."""
        self._write_unversioned("script")
        with patch("gateway.service.connector_factory") as factory:
            with self.assertRaises(LegacyStateError):
                GatewayService(self._config())
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
