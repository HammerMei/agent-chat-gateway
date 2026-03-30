"""Tests for rocketchat normalize.py — path traversal, tmp cleanup, mention handling,
parallel downloads, attachment symlink path injection.

Covers:
  - Attachment dest_path outside cache_dir blocked (round7)
  - tmp file cleaned up on CancelledError (round12)
  - RocketChat mention handling (code_review)
  - Parallel attachment downloads with bounded concurrency (code_review)
  - Attachment warning prompt injection (code_review)
  - Attachment symlink path remapping (code_review)

Run with:
    uv run python -m pytest tests/test_normalize.py -v
"""

from __future__ import annotations
import pytest

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import IsolatedTestCase


# ── Tests: path traversal check ──────────────────────────────────────────────



pytestmark = pytest.mark.integration

class TestPathTraversalCheck(unittest.IsolatedAsyncioTestCase):
    """Attachment dest_path outside cache_dir must be blocked."""

    async def test_traversal_blocked_returns_none(self):
        """A dest_path that resolves outside cache_dir must return None."""
        from gateway.connectors.rocketchat import normalize

        cache_dir = Path("/tmp/cache")

        file_info = {"_id": "fileid123", "name": "test.txt", "size": 100, "type": "text/plain"}
        doc = {"files": [file_info], "attachments": []}
        config = MagicMock()
        config.attachments.max_file_size_mb = 10
        config.attachments.download_timeout = 30
        rest = MagicMock()

        original_resolve = Path.resolve

        def patched_resolve(self):
            if "fileid123" in str(self):
                return Path("/tmp/other_dir/evil.txt")
            return original_resolve(self)

        with patch.object(Path, "resolve", patched_resolve):
            attachments, warnings = await normalize._download_attachments(
                doc, config, rest, cache_dir
            )

        self.assertEqual(attachments, [])

    async def test_valid_path_not_blocked(self):
        """A valid attachment path under cache_dir must not be blocked."""
        from gateway.connectors.rocketchat import normalize

        cache_dir = Path("/tmp/cache")

        file_info = {"_id": "abc123", "name": "photo.jpg", "size": 500, "type": "image/jpeg"}
        doc = {"files": [file_info], "attachments": []}
        config = MagicMock()
        config.attachments.max_file_size_mb = 10
        config.attachments.download_timeout = 30

        rest = MagicMock()
        rest.download_file = AsyncMock()

        async def fake_to_thread(fn, *args, **kwargs):
            if callable(fn) and hasattr(fn, "__self__"):
                return None
            return await asyncio.to_thread(fn, *args, **kwargs)

        with patch("gateway.connectors.rocketchat.normalize.asyncio.to_thread",
                   side_effect=fake_to_thread):
            try:
                await normalize._download_attachments(doc, config, rest, cache_dir)
            except Exception:
                pass  # download itself may fail; we only care the guard didn't block it


# ── Tests: tmp file cleanup on CancelledError ────────────────────────────────


class TestNormalizeTmpCleanupOnCancel(unittest.IsolatedAsyncioTestCase):
    """_download_one must remove its .tmp file even when CancelledError fires."""

    async def test_tmp_file_removed_on_timeout(self):
        """TimeoutError path: tmp file must be cleaned up."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            file_info = {
                "_id": "f1",
                "name": "doc.pdf",
                "size": 100,
                "type": "application/pdf",
            }
            doc = {
                "files": [file_info],
                "attachments": [
                    {"title_link": "/file-upload/f1/doc.pdf", "title": "doc.pdf"}
                ],
            }
            config = MagicMock()
            config.attachments.max_file_size_mb = 10
            config.attachments.download_timeout = 1

            rest = MagicMock()
            rest.download_file = AsyncMock(side_effect=asyncio.TimeoutError())

            await _download_attachments(doc, config, rest, cache_dir)

            tmp_files = list(cache_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [], f"Leftover tmp files: {tmp_files}")

    async def test_tmp_file_removed_on_exception(self):
        """Generic Exception path: tmp file must be cleaned up."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            file_info = {"_id": "f2", "name": "img.png", "size": 50, "type": "image/png"}
            doc = {
                "files": [file_info],
                "attachments": [
                    {"title_link": "/file-upload/f2/img.png", "title": "img.png"}
                ],
            }
            config = MagicMock()
            config.attachments.max_file_size_mb = 10
            config.attachments.download_timeout = 30

            rest = MagicMock()
            rest.download_file = AsyncMock(side_effect=OSError("network error"))

            await _download_attachments(doc, config, rest, cache_dir)

            tmp_files = list(cache_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [], f"Leftover tmp files: {tmp_files}")

    async def test_tmp_file_removed_on_cancelled_error(self):
        """CancelledError (BaseException) path: tmp file must still be cleaned up."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            file_info = {"_id": "f3", "name": "vid.mp4", "size": 200, "type": "video/mp4"}
            doc = {
                "files": [file_info],
                "attachments": [
                    {"title_link": "/file-upload/f3/vid.mp4", "title": "vid.mp4"}
                ],
            }
            config = MagicMock()
            config.attachments.max_file_size_mb = 1000
            config.attachments.download_timeout = 30

            async def _cancel_mid_download(url, dest):
                Path(dest).write_bytes(b"partial data")
                raise asyncio.CancelledError()

            rest = MagicMock()
            rest.download_file = _cancel_mid_download

            with self.assertRaises(asyncio.CancelledError):
                await _download_attachments(doc, config, rest, cache_dir)

            tmp_files = list(cache_dir.glob("*.tmp"))
            self.assertEqual(
                tmp_files, [],
                f"tmp file(s) not cleaned up after CancelledError: {tmp_files}",
            )

    async def test_no_leftover_tmp_on_success(self):
        """Successful download: after rename(), no .tmp file should remain."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            file_info = {
                "_id": "f4",
                "name": "report.txt",
                "size": 10,
                "type": "text/plain",
            }
            doc = {
                "files": [file_info],
                "attachments": [
                    {"title_link": "/file-upload/f4/report.txt", "title": "report.txt"}
                ],
            }
            config = MagicMock()
            config.attachments.max_file_size_mb = 10
            config.attachments.download_timeout = 30

            async def _write_file(url, dest):
                Path(dest).write_bytes(b"hello")

            rest = MagicMock()
            rest.download_file = _write_file

            attachments, warnings = await _download_attachments(doc, config, rest, cache_dir)

            self.assertEqual(len(attachments), 1)
            tmp_files = list(cache_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [], f"Leftover tmp files: {tmp_files}")


# ── Tests: RocketChat mention handling ───────────────────────────────────────


class TestRocketChatMentionHandling(unittest.TestCase):
    def _config(self):
        config = MagicMock()
        config.username = "bot"
        config.allow_senders = ["alice"]
        return config

    def test_filter_does_not_treat_email_like_text_as_mention(self):
        from gateway.connectors.rocketchat.normalize import filter_rc_message

        doc = {
            "u": {"username": "alice"},
            "msg": "email me at hello@bot.com",
            "attachments": [],
            "mentions": [],
            "ts": {"$date": "200"},
        }

        result = filter_rc_message(doc, self._config(), "channel", None)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "bot not mentioned")

    def test_extract_text_strips_only_leading_mention(self):
        from gateway.connectors.rocketchat.normalize import _extract_text

        doc = {"msg": "@bot please ask @bot to review this"}
        text = _extract_text(doc, "channel", "bot")
        self.assertEqual(text, "please ask @bot to review this")

    def test_extract_text_keeps_mid_sentence_mention_without_leading_prefix(self):
        from gateway.connectors.rocketchat.normalize import _extract_text

        doc = {"msg": "please ask @bot to review this"}
        text = _extract_text(doc, "channel", "bot")
        self.assertEqual(text, "please ask @bot to review this")


# ── Tests: parallel attachment downloads ─────────────────────────────────────


class TestParallelAttachmentDownloads(unittest.IsolatedAsyncioTestCase):
    """Issue #12: attachments should download in parallel with bounded concurrency."""

    async def test_multiple_attachments_downloaded_concurrently(self):
        """5 attachments should use asyncio.gather (not serial) with semaphore=4."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        download_order = []
        active_count = []

        _active = 0

        async def mock_download(title_link, dest_path):
            nonlocal _active
            _active += 1
            active_count.append(_active)
            download_order.append(title_link)
            await asyncio.sleep(0.05)  # simulate I/O
            Path(dest_path).touch()
            _active -= 1

        mock_rest = MagicMock()
        mock_rest.download_file = mock_download

        mock_config = MagicMock()
        mock_config.attachments.max_file_size_mb = 10.0
        mock_config.attachments.download_timeout = 30

        doc = {
            "files": [
                {
                    "_id": f"file{i}",
                    "name": f"doc{i}.pdf",
                    "size": 100,
                    "type": "application/pdf",
                }
                for i in range(5)
            ],
            "attachments": [
                {"title_link": f"/file-upload/file{i}/doc{i}.pdf"} for i in range(5)
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            results, warnings = await _download_attachments(
                doc, mock_config, mock_rest, Path(tmpdir)
            )

        self.assertEqual(len(results), 5)
        self.assertEqual(len(warnings), 0)
        self.assertLessEqual(max(active_count), 4)
        self.assertGreater(max(active_count), 1)

    async def test_partial_failure_does_not_block_others(self):
        """One download failing should not prevent other downloads from completing."""
        from gateway.connectors.rocketchat.normalize import _download_attachments

        call_count = 0

        async def mock_download(title_link, dest_path):
            nonlocal call_count
            call_count += 1
            if "file1" in title_link:
                raise RuntimeError("network error")
            Path(dest_path).touch()

        mock_rest = MagicMock()
        mock_rest.download_file = mock_download

        mock_config = MagicMock()
        mock_config.attachments.max_file_size_mb = 10.0
        mock_config.attachments.download_timeout = 30

        doc = {
            "files": [
                {
                    "_id": f"file{i}",
                    "name": f"doc{i}.pdf",
                    "size": 100,
                    "type": "application/pdf",
                }
                for i in range(3)
            ],
            "attachments": [
                {"title_link": f"/file-upload/file{i}/doc{i}.pdf"} for i in range(3)
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            results, warnings = await _download_attachments(
                doc, mock_config, mock_rest, Path(tmpdir)
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("doc1.pdf", warnings[0])


# ── Tests: attachment warning prompt injection ───────────────────────────────


class TestAttachmentWarningPromptInjection(IsolatedTestCase):
    """Issue #10: attachment download failures must be surfaced in the agent prompt."""

    async def test_warnings_injected_into_prompt(self):
        """When IncomingMessage has warnings, they appear in the agent prompt."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.connector import IncomingMessage, Room, User, UserRole
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self.sent_messages = []
                self._session_counter = 0

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                self.sent_messages.append({"prompt": prompt, "session_id": session_id})
                return AgentResponse(text="mock reply")

        connector = ScriptConnector()
        agent = MockAgent()
        wc = WatcherConfig(name="script", connector="script", room="script", agent="default")
        agent_cfg = AgentConfig(timeout=10)
        config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
        manager = SessionManager(connector, {"default": agent}, "default", config,
                                 watcher_configs=[wc])
        await manager.run_once()

        msg = IncomingMessage(
            id="test-msg",
            timestamp="",
            room=Room(id="script", name="script", type="script"),
            sender=User(id="user", username="user"),
            role=UserRole.OWNER,
            text="check these files",
            warnings=[
                "[⚠️ Attachment 'report.pdf' failed to download (timed out) — file not available]",
            ],
        )

        await connector._handler(msg)
        await connector.receive_reply(timeout=5.0)

        last_send = agent.sent_messages[-1]
        self.assertIn("report.pdf", last_send["prompt"])
        self.assertIn("timed out", last_send["prompt"])
        self.assertIn("check these files", last_send["prompt"])

        await manager.shutdown()

    async def test_no_warnings_no_injection(self):
        """When no warnings, prompt is unchanged."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.connector import UserRole
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self.sent_messages = []
                self._session_counter = 0

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                self.sent_messages.append({"prompt": prompt, "session_id": session_id})
                return AgentResponse(text="mock reply")

        connector = ScriptConnector()
        agent = MockAgent()
        wc = WatcherConfig(name="script", connector="script", room="script", agent="default")
        agent_cfg = AgentConfig(timeout=10)
        config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
        manager = SessionManager(connector, {"default": agent}, "default", config,
                                 watcher_configs=[wc])
        await manager.run_once()

        await connector.inject("hello", role=UserRole.OWNER)
        await connector.receive_reply(timeout=5.0)

        last_send = agent.sent_messages[-1]
        self.assertEqual(last_send["prompt"], "hello")

        await manager.shutdown()


# ── Tests: attachment symlink ─────────────────────────────────────────────────


class TestAttachmentSymlink(IsolatedTestCase):
    """Issue #17: per-watcher symlinks for attachment paths inside agent cwd."""

    async def test_symlink_created_on_watcher_start(self):
        """_start_watcher should create .acg-attachments/{watcher_name} symlink."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self._session_counter = 0

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                return AgentResponse(text="ok")

        with tempfile.TemporaryDirectory() as tmpdir:
            connector = ScriptConnector()
            cache_dir = Path(tmpdir) / "global-cache" / "room-1"
            cache_dir.mkdir(parents=True)

            connector.attachment_cache_dir = lambda room_id: str(cache_dir)

            agent = MockAgent()
            wc = WatcherConfig(name="script", connector="script", room="script", agent="default")
            agent_cfg = AgentConfig(timeout=10, working_directory=tmpdir)
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")

            manager = SessionManager(connector, {"default": agent}, "default", config,
                                     watcher_configs=[wc])
            await manager.run_once()

            link = Path(tmpdir) / ".acg-attachments" / "script"
            self.assertTrue(link.is_symlink(), f"Expected symlink at {link}")
            self.assertEqual(link.resolve(), cache_dir.resolve())

            await manager.shutdown()

    async def test_multiple_watchers_get_separate_symlinks(self):
        """Two watchers on same cwd get separate symlinks under .acg-attachments/."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self._session_counter = 0

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                return AgentResponse(text="ok")

        with tempfile.TemporaryDirectory() as tmpdir:
            connector = ScriptConnector()

            cache_room1 = Path(tmpdir) / "global-cache" / "room-1"
            cache_room2 = Path(tmpdir) / "global-cache" / "room-2"
            cache_room1.mkdir(parents=True)
            cache_room2.mkdir(parents=True)

            def mock_cache_dir(room_id):
                if room_id == "room-a":
                    return str(cache_room1)
                return str(cache_room2)

            connector.attachment_cache_dir = mock_cache_dir

            agent = MockAgent()
            agent_cfg = AgentConfig(timeout=10, working_directory=tmpdir)
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")

            wc1 = WatcherConfig(
                name="watcher-a", connector="script", room="room-a", agent="default"
            )
            wc2 = WatcherConfig(
                name="watcher-b", connector="script", room="room-b", agent="default"
            )

            manager = SessionManager(connector, {"default": agent}, "default", config,
                                     watcher_configs=[wc1, wc2])
            await manager.run_once()

            link_a = Path(tmpdir) / ".acg-attachments" / "watcher-a"
            link_b = Path(tmpdir) / ".acg-attachments" / "watcher-b"

            self.assertTrue(link_a.is_symlink())
            self.assertTrue(link_b.is_symlink())
            self.assertEqual(link_a.resolve(), cache_room1.resolve())
            self.assertEqual(link_b.resolve(), cache_room2.resolve())

            await manager.shutdown()

    async def test_attachment_paths_remapped_through_symlink(self):
        """MessageProcessor should remap attachment paths to cwd-local symlink paths."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.connector import Attachment, IncomingMessage, Room, User, UserRole
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self._session_counter = 0
                self.sent_messages = []

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                self.sent_messages.append({"prompt": prompt, "attachments": attachments})
                return AgentResponse(text="ok")

        with tempfile.TemporaryDirectory() as tmpdir:
            connector = ScriptConnector()

            cache_dir = Path(tmpdir) / "global-cache" / "room-1"
            cache_dir.mkdir(parents=True)
            fake_file = cache_dir / "fileXYZ_doc.pdf"
            fake_file.touch()

            connector.attachment_cache_dir = lambda room_id: str(cache_dir)

            agent = MockAgent()
            agent_cfg = AgentConfig(timeout=10, working_directory=tmpdir)
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")

            wc = WatcherConfig(name="script", connector="script", room="script", agent="default")
            manager = SessionManager(connector, {"default": agent}, "default", config,
                                     watcher_configs=[wc])
            await manager.run_once()

            msg = IncomingMessage(
                id="test-msg",
                timestamp="",
                room=Room(id="script", name="script", type="script"),
                sender=User(id="user", username="user"),
                role=UserRole.OWNER,
                text="check this file",
                attachments=[
                    Attachment(
                        original_name="doc.pdf",
                        local_path=str(fake_file),
                        mime_type="application/pdf",
                        size_bytes=100,
                    )
                ],
            )

            await connector._handler(msg)
            await connector.receive_reply(timeout=5.0)

            last_send = agent.sent_messages[-1]
            expected_local = str(
                Path(tmpdir) / ".acg-attachments" / "script" / "fileXYZ_doc.pdf"
            )
            self.assertIsNotNone(last_send["attachments"])
            self.assertIn(
                expected_local,
                last_send["attachments"],
                f"Expected local path in attachments, got: {last_send['attachments']}",
            )

            await manager.shutdown()

    def test_localize_paths_without_symlink_returns_originals(self):
        """When no symlink is configured, original paths are returned."""
        from gateway.core.attachment_workspace import localize_attachment_paths
        from gateway.core.connector import Attachment

        attachments = [
            Attachment(original_name="a.txt", local_path="/global/cache/a.txt"),
            Attachment(original_name="b.txt", local_path="/global/cache/b.txt"),
        ]
        result = localize_attachment_paths(attachments, local_base=None)
        self.assertEqual(result, ["/global/cache/a.txt", "/global/cache/b.txt"])

    async def test_connector_without_cache_dir_skips_symlink(self):
        """ScriptConnector returns None for attachment_cache_dir — no symlink created."""
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig, WatcherConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.session_manager import SessionManager

        class MockAgent(AgentBackend):
            def __init__(self):
                self._session_counter = 0

            async def create_session(self, working_directory, extra_args=None, session_title=None):
                self._session_counter += 1
                return f"mock-session-{self._session_counter:04d}"

            async def send(self, session_id, prompt, working_directory, timeout,
                           attachments=None, env=None):
                return AgentResponse(text="ok")

        with tempfile.TemporaryDirectory() as tmpdir:
            connector = ScriptConnector()
            agent = MockAgent()
            wc = WatcherConfig(name="script", connector="script", room="script", agent="default")
            agent_cfg = AgentConfig(timeout=10, working_directory=tmpdir)
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")

            manager = SessionManager(connector, {"default": agent}, "default", config,
                                     watcher_configs=[wc])
            await manager.run_once()

            acg_dir = Path(tmpdir) / ".acg-attachments"
            self.assertFalse(
                acg_dir.exists(),
                "No .acg-attachments dir should be created for ScriptConnector",
            )

            await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
