"""Two watchers on one connector and one room: refused, and refused early (§4.1).

This file used to assert the opposite — that such a pair each got their own durable
instructions file — because the configuration was expressible and the dispatcher kept a
*list* of processors per room, so both received every message. That shape is gone: the
index holds one processor per room, config load rejects the pair, and the second claim
raises.

The test is rewritten rather than deleted because the *case* still matters; only the
correct outcome changed. What it pins now is that the refusal happens before the
expensive half of starting a watcher, which is not automatic: the durable-instructions
file is written during context injection, several steps before the room is claimed, so a
refusal at claim time would leave a file (and a session, and a subscription) behind for a
watcher that never ran.

`watcher_prompt_key` keeps its three-part key. The collision it was written for can no
longer occur, but the extra specificity is harmless and removing it would be churn in a
key that names files on disk.

Run with:
    uv run python -m pytest tests/integration/test_same_room_watchers.py -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.connectors.script import ScriptConnector
from gateway.core.config import CoreConfig
from gateway.core.session_manager import SessionManager
from gateway.core.state import StateFilter

pytestmark = pytest.mark.integration


class _RecordingAgent(AgentBackend):
    """Writes durable instructions to disk the way the real adapters do."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._sessions = 0

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        self._sessions += 1
        return f"ses_{self._sessions}"

    async def ensure_durable_instructions(
        self, session_id, working_directory, timeout, content, *, path_key,
        already_delivered,
    ):
        prompts = self._root / "system-prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        path = prompts / f"{path_key}.md"
        path.write_text(content)
        return str(path)

    async def send(self, session_id, prompt, working_directory, timeout, **kw):
        return AgentResponse(text="ok", session_id=session_id)


class TestTwoWatchersOneRoom(unittest.IsolatedAsyncioTestCase):
    async def test_the_second_watcher_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector = ScriptConnector()
            agent = _RecordingAgent(root)
            agent_cfg = AgentConfig(timeout=10, working_directory=str(root))
            config = CoreConfig(
                agents={"a1": agent_cfg, "a2": agent_cfg}, default_agent="a1"
            )
            watchers = [
                WatcherConfig(name="w-a1", connector="script", room="script", agent="a1"),
                WatcherConfig(name="w-a2", connector="script", room="script", agent="a2"),
            ]
            manager = SessionManager(
                connector, {"a1": agent, "a2": agent}, "a1", config,
                watcher_configs=watchers,
            )
            errors = await manager.run_once()

            self.assertTrue(
                any("w-a2" in e for e in errors),
                f"the second watcher on the room should have failed to start: {errors}",
            )
            # Which watcher is *serving* the room is a question about processors,
            # not about records — `list_watchers()` answers the second one.
            # Scanned over every row rather than a hardcoded pair, so an
            # unexpected third watcher serving the room still fails this.
            serving = [
                w["watcher_name"]
                for w in manager.list_watchers(StateFilter.ALL)
                if manager.get_processor(w["watcher_name"]) is not None
            ]
            self.assertEqual(
                serving, ["w-a1"],
                "exactly one watcher may serve a room on one connector",
            )
            await manager.shutdown()

    async def test_the_refusal_costs_nothing(self):
        """Refused before the work, not after it.

        The claim on the room is the last step of starting a watcher; reaching it means
        a session was created, context injected and its durable-instructions file
        written, all to be undone. A `holder()` check right after the room resolves
        makes the refusal cheap — and this asserts the consequence (one file on disk)
        rather than the check, so moving the check back would fail here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector = ScriptConnector()
            agent = _RecordingAgent(root)
            agent_cfg = AgentConfig(timeout=10, working_directory=str(root))
            config = CoreConfig(
                agents={"a1": agent_cfg, "a2": agent_cfg}, default_agent="a1"
            )
            manager = SessionManager(
                connector, {"a1": agent, "a2": agent}, "a1", config,
                watcher_configs=[
                    WatcherConfig(name="w-a1", connector="script", room="script", agent="a1"),
                    WatcherConfig(name="w-a2", connector="script", room="script", agent="a2"),
                ],
            )
            await manager.run_once()

            written = sorted((root / "system-prompts").glob("*.md"))
            self.assertEqual(
                len(written), 1,
                f"the refused watcher left work behind: {[p.name for p in written]}",
            )
            self.assertEqual(
                agent._sessions, 1,
                "the refused watcher created a session it will never use",
            )
            await manager.shutdown()

    async def test_one_watcher_writes_one_file(self):
        """The control: the fix must not have made the key per-something-spurious, which
        would show up as a second file for a single watcher."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector = ScriptConnector()
            agent = _RecordingAgent(root)
            agent_cfg = AgentConfig(timeout=10, working_directory=str(root))
            config = CoreConfig(agents={"a1": agent_cfg}, default_agent="a1")
            manager = SessionManager(
                connector, {"a1": agent}, "a1", config,
                watcher_configs=[
                    WatcherConfig(
                        name="only", connector="script", room="script", agent="a1"
                    )
                ],
            )
            await manager.run_once()
            self.assertEqual(len(list((root / "system-prompts").glob("*.md"))), 1)
            await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
