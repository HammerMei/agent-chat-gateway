"""Two watchers on one room must not share a durable-instructions file.

The static shape permits binding different agents to one connector+room: the config
loads, and `MessageDispatcher` keeps a *list* of processors per room, so a message
reaches both. Their durable instructions differ — the content is built from the agent
and the watcher's own context files — so the file cannot be keyed on the room alone
while that configuration is expressible.

Keying it on the room made the second watcher's write overwrite the first one's identity
and context, after which both processors used the overwritten file on every turn. Nothing
raised. This asserts the end result rather than only that two keys differ, because "the
keys differ" would still pass if a call site used the wrong one — which is exactly the
mistake the first version of this change made in two places.

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
    async def test_each_watcher_gets_its_own_prompt_file(self):
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
            await manager.run_once()

            written = sorted((root / "system-prompts").glob("*.md"))
            self.assertEqual(
                len(written), 2,
                "two watchers on one room shared a prompt file — the second write "
                f"overwrote the first: {[p.name for p in written]}",
            )
            # And the two files are not the same inode by another name.
            self.assertEqual(len({p.name for p in written}), 2)

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
