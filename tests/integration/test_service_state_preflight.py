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


class TestBackendSignaturePreflight(unittest.TestCase):
    """A backend left on the pre-rename signature must fail with an actionable message.

    `ensure_durable_instructions`'s `watcher_name` parameter became `path_key`, and the two
    are not interchangeable — the value is now scoped to the watcher in a room. A custom
    backend still on the old spelling would raise `TypeError: unexpected keyword argument
    'path_key'` at the first watcher start, deep inside the lifecycle, which rolls the
    startup back and says nothing about what to change.

    A compatibility shim was declined rather than overlooked: registering a backend
    requires editing `service.py`, so a custom one is a fork rather than a plugin, and a
    fork rebasing onto this branch already meets a removed config field and a refused state
    format. Accepting both spellings would keep two names for one parameter alive in a
    contract the rename exists to disambiguate. What the shim would really have bought is
    the better message — which this provides, and earlier.
    """

    def test_the_old_signature_is_refused_by_name(self):
        from gateway.agents import AgentBackend, check_backend_signatures

        class _Legacy(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content, *,
                watcher_name, already_delivered,
            ):
                return None

        with self.assertRaises(TypeError) as cm:
            check_backend_signatures({"legacy": _Legacy()})
        msg = str(cm.exception)
        self.assertIn("legacy", msg)
        self.assertIn("watcher_name", msg)
        self.assertIn("path_key", msg)
        self.assertIn("room_path_key", msg, "must warn against the wrong substitution")

    def test_the_current_signature_passes(self):
        from gateway.agents import AgentBackend, check_backend_signatures

        class _Current(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content, *,
                path_key, already_delivered,
            ):
                return None

        check_backend_signatures({"ok": _Current()})

    def test_a_backend_that_does_not_override_it_is_left_alone(self):
        """Not overriding is legitimate — the base raises with its own message, and many
        test backends never exercise this path. The preflight must not force them to
        implement it just to start."""
        from gateway.agents import AgentBackend, check_backend_signatures

        class _NoOverride(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

        check_backend_signatures({"bare": _NoOverride()})

    def test_the_service_actually_runs_the_preflight(self):
        """The wiring, not the function.

        Every test above calls `check_backend_signatures` directly, so removing the call
        from `GatewayService.__init__` failed none of them — the same gap as asserting two
        keys differ while a call site uses the wrong one. This constructs the service with
        a legacy backend and requires the refusal.
        """
        from gateway.agents import AgentBackend

        class _Legacy(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content, *,
                watcher_name, already_delivered,
            ):
                return None

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        runtime = root / "runtime"
        runtime.mkdir()
        path = root / "config.yaml"
        path.write_text(textwrap.dedent(f"""\
            connectors:
              - name: script
                type: script
            agents:
              default:
                type: claude
                working_directory: {root}
            watchers:
              - name: w1
                connector: script
                room: script
        """))

        with patch.object(state_mod, "RUNTIME_DIR", runtime), \
             patch("gateway.service._build_agent_backend", return_value=_Legacy()):
            cfg = GatewayConfig.from_file(str(path))
            with self.assertRaises(TypeError) as cm:
                GatewayService(cfg)
        self.assertIn("watcher_name", str(cm.exception))

    def test_a_positional_only_path_key_is_refused(self):
        """Presence is not enough — the parameter has to be callable by keyword.

        The caller passes `path_key=...`, so a positional-only declaration satisfies a
        membership test and then raises "got some positional-only arguments passed as
        keyword arguments" at the first watcher start: exactly the raw TypeError this
        preflight exists to replace. A check that lets its own failure mode through in a
        describable case is not doing its one job.
        """
        from gateway.agents import AgentBackend, check_backend_signatures

        class _PositionalOnly(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content, path_key, /, *,
                already_delivered,
            ):
                return None

        with self.assertRaises(TypeError) as cm:
            check_backend_signatures({"posonly": _PositionalOnly()})
        msg = str(cm.exception)
        self.assertIn("positional-only", msg)
        self.assertIn("keyword-only", msg, "must say what to change it to")

    def test_a_positional_or_keyword_path_key_is_accepted(self):
        """The permissive-but-callable case: not keyword-only, but the keyword call
        works, so refusing it would reject a working backend."""
        from gateway.agents import AgentBackend, check_backend_signatures

        class _PositionalOrKeyword(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content, path_key,
                already_delivered=False,
            ):
                return None

        check_backend_signatures({"poskw": _PositionalOrKeyword()})

    def test_a_backend_taking_kwargs_is_accepted(self):
        """A `**kwargs` override cannot be judged by parameter name and is not the defect
        this looks for; forcing it to fail would refuse a working implementation."""
        from gateway.agents import AgentBackend, check_backend_signatures

        class _Kwargs(AgentBackend):
            async def create_session(self, *a, **kw):
                return "s"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def ensure_durable_instructions(self, *a, **kw):
                return None

        check_backend_signatures({"flexible": _Kwargs()})


if __name__ == "__main__":
    unittest.main()
