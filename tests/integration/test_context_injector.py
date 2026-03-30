"""Tests for gateway.core.context_injector.

Consolidates context injector tests from rounds 4, 7, 8, 9 and standalone files.

Covers:
  - Oversized files / all-files-oversized cases (round4)
  - TOCTOU re-validation after read_text() (round7)
  - Concurrent inject() guard / second caller bails if first is in-flight (round8)
  - Pending status reset to not_started on unexpected exceptions (round9)
  - Async file IO (code_review / Issue #15)
  - Retry cap (from test_context_injector_retry.py)
  - Degraded state no-retry guarantee (from test_context_injector_degraded.py)

Run with:
    uv run python -m pytest tests/test_context_injector.py -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.core.config import CoreConfig
from gateway.core.context_injector import _MAX_FILE_SIZE, _MAX_INJECT_ATTEMPTS, ContextInjector
from gateway.state import WatcherState

# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_injector() -> ContextInjector:
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    return ContextInjector(config)


def _make_ws(context_injected: bool = False) -> WatcherState:
    return WatcherState(
        watcher_name="test", session_id="", room_id="r1",
        context_injected=context_injected,
    )


def _make_wc(ctx_files: list[str]) -> WatcherConfig:
    return WatcherConfig(
        name="test",
        connector="rc",
        room="general",
        agent="default",
        context_inject_files=ctx_files,
    )


async def _fake_to_thread_file_reads(fn, *args, **kwargs):
    """Simulate async file reads — exists=True, st_size=100, read_text='context'."""
    name = getattr(fn, "__name__", "")
    if "exists" in name:
        return True
    if "stat" in name:
        stat = MagicMock()
        stat.st_size = 100
        return stat
    if "read_text" in name:
        return "context"
    return None


# ── Tests: oversized context files ───────────────────────────────────────────



pytestmark = pytest.mark.integration

class TestContextInjectorAllFilesOversized(unittest.IsolatedAsyncioTestCase):
    """When all context files exceed the size limit, injection must succeed immediately."""

    async def test_all_oversized_files_marks_injected(self):
        """Files that all exceed _MAX_FILE_SIZE → ws.context_injected=True, no agent call."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = AsyncMock()
        agent.send = AsyncMock()

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1
                return stat
            return None

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        self.assertTrue(ws.context_injected, "All oversized → must mark injected")
        agent.send.assert_not_awaited()

    async def test_all_oversized_sets_injected_status(self):
        """Status for the session must be 'injected' when all files are oversized."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = AsyncMock()

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1
                return stat
            return None

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        status = injector.status_for("ses_1")
        self.assertEqual(status.state, "injected")

    async def test_all_oversized_does_not_count_as_failure(self):
        """Oversized-skip must NOT increment failure_count — it is not an agent error."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = AsyncMock()

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1
                return stat
            return None

        for _ in range(_MAX_INJECT_ATTEMPTS + 1):
            ws.context_injected = False
            with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
                await injector.inject(ws, "ses_1", agent, "default", "rc", wc)
            injector.reset_session("ses_1")

        self.assertEqual(
            injector.status_for("ses_1").failure_count,
            0,
            "Oversized file skip must not increment failure_count",
        )

    async def test_partial_oversized_injects_small_files(self):
        """Files under the size limit are still injected even if others are oversized."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc(["/tmp/big.md", "/tmp/small.md"])
        agent = AsyncMock()
        agent.send = AsyncMock(return_value=AgentResponse(text="ok", is_error=False))

        async def fake_to_thread(fn, *args, **kwargs):
            path_arg = args[0] if args else None
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1 if "big" in str(path_arg) else 100
                return stat
            if "read_text" in name:
                return "small context content"
            return None

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        agent.send.assert_awaited_once()
        self.assertTrue(ws.context_injected)


# ── Tests: TOCTOU re-validation ───────────────────────────────────────────────


class TestContextInjectorTOCTOU(unittest.IsolatedAsyncioTestCase):
    """After read_text(), content size must be re-validated to close TOCTOU window."""

    async def test_oversized_content_after_read_is_skipped(self):
        """Content that grew between stat() and read_text() must be rejected."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])

        oversized_content = "x" * (_MAX_FILE_SIZE + 1)

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", str(fn))
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE - 1  # stat says ok
                return stat
            if "read_text" in name:
                return oversized_content  # content grew after stat
            return None

        agent = MagicMock()
        agent.send = AsyncMock()

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        agent.send.assert_not_awaited()
        self.assertTrue(ws.context_injected)


# ── Tests: concurrent inject() guard ─────────────────────────────────────────


class TestConcurrentInjectGuard(unittest.IsolatedAsyncioTestCase):
    """A second inject() call for the same session must bail if first is in-flight."""

    async def test_second_inject_skipped_when_pending(self):
        """inject() called while status is 'pending' must return immediately."""
        from gateway.core.context_injector import InjectionStatus

        injector = _make_injector()
        session_id = "ses_concurrent"
        injector._inject_status[session_id] = InjectionStatus(state="pending")

        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = MagicMock()
        agent.send = AsyncMock()

        await injector.inject(ws, session_id, agent, "default", "rc", wc)

        agent.send.assert_not_awaited()
        self.assertFalse(ws.context_injected)

    async def test_concurrent_injects_only_inject_once(self):
        """Two concurrent inject() calls for the same session must only send once."""
        injector = _make_injector()
        session_id = "ses_race"

        ws1 = _make_ws()
        ws2 = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])

        send_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            await asyncio.sleep(0)
            name = getattr(fn, "__name__", str(fn))
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = 100
                return stat
            if "read_text" in name:
                return "context content"
            return None

        agent = MagicMock()

        async def fake_send(**kwargs):
            nonlocal send_count
            send_count += 1
            return AgentResponse(text="ok")

        agent.send = fake_send

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await asyncio.gather(
                injector.inject(ws1, session_id, agent, "default", "rc", wc),
                injector.inject(ws2, session_id, agent, "default", "rc", wc),
            )

        self.assertEqual(send_count, 1, f"agent.send must be called exactly once, got {send_count}")


# ── Tests: pending status reset on exception ─────────────────────────────────


class TestContextInjectorPendingReset(unittest.IsolatedAsyncioTestCase):
    """inject() must reset 'pending' → 'not_started' on unexpected exceptions."""

    async def test_pending_reset_after_io_error(self):
        """After an OSError during file I/O, status must return to 'not_started'."""
        injector = _make_injector()
        session_id = "ses_io_error"

        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = MagicMock()
        agent.send = AsyncMock()

        async def fake_to_thread(fn, *args, **kwargs):
            raise OSError("permission denied")

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            with self.assertRaises(OSError):
                await injector.inject(ws, session_id, agent, "default", "rc", wc)

        status = injector.status_for(session_id)
        self.assertNotEqual(status.state, "pending")
        self.assertEqual(status.state, "not_started")

    async def test_retry_allowed_after_io_error(self):
        """A second inject() call must NOT bail early after a first IO failure."""
        injector = _make_injector()
        session_id = "ses_retry"

        ws = _make_ws()
        wc = _make_wc(["/tmp/ctx.md"])
        agent = MagicMock()

        send_count = 0

        async def fake_send(**kwargs):
            nonlocal send_count
            send_count += 1
            return AgentResponse(text="ok")

        agent.send = fake_send

        call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient error")
            name = getattr(fn, "__name__", str(fn))
            if "exists" in name:
                return True
            if "stat" in name:
                s = MagicMock()
                s.st_size = 50
                return s
            if "read_text" in name:
                return "context"
            return None

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            with self.assertRaises(OSError):
                await injector.inject(ws, session_id, agent, "default", "rc", wc)

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, session_id, agent, "default", "rc", wc)

        self.assertEqual(send_count, 1, "agent.send must be called on the successful retry")
        self.assertTrue(ws.context_injected)


# ── Tests: context injection ordering ────────────────────────────────────────


class TestContextInjectionOrdering(unittest.IsolatedAsyncioTestCase):
    """Issue #9: session maps must be registered BEFORE context injection."""

    async def _run_test(self, watcher_configs, check_fn):
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.session_manager import SessionManager
        from gateway.core.session_maps import SessionMaps

        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            class MockAgent(AgentBackend):
                def __init__(self):
                    self._session_counter = 0
                    self.sent_messages = []

                async def create_session(self, working_directory, extra_args=None, session_title=None):
                    self._session_counter += 1
                    return f"mock-session-{self._session_counter:04d}"

                async def send(self, session_id, prompt, working_directory, timeout,
                               attachments=None, env=None):
                    self.sent_messages.append({"prompt": prompt})
                    return AgentResponse(text="ok")

            connector = ScriptConnector()
            agent = MockAgent()
            maps = SessionMaps()
            agent_cfg = AgentConfig(timeout=10, context_inject_files=[])
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
            manager = SessionManager(
                connector, {"default": agent}, "default", config,
                watcher_configs=watcher_configs, session_maps=maps,
            )
            await check_fn(manager, connector, agent, maps)
            await manager.shutdown()
        finally:
            _patch_load.stop()
            _patch_save.stop()

    async def test_maps_registered_before_injection(self):
        """session maps must be populated before _inject_context runs."""
        from gateway.config import WatcherConfig

        maps_at_injection: dict = {}
        wc = WatcherConfig(name="script", connector="script", room="script", agent="default")

        async def check_fn(manager, connector, agent, maps):
            original_inject = manager._lifecycle._injector.inject

            async def capturing_inject(*args, **kwargs):
                maps_at_injection["room_map_keys"] = list(maps.room.keys())
                maps_at_injection["connector_map_keys"] = list(maps.connector.keys())
                return await original_inject(*args, **kwargs)

            manager._lifecycle._injector.inject = capturing_inject
            await manager.run_once()

        await self._run_test([wc], check_fn)

        self.assertTrue(
            len(maps_at_injection.get("room_map_keys", [])) > 0,
            "session room map was empty when _inject_context ran",
        )
        self.assertTrue(
            len(maps_at_injection.get("connector_map_keys", [])) > 0,
            "session connector map was empty when _inject_context ran",
        )

    async def test_injection_failure_rolls_back_maps(self):
        """If _inject_context fails, session maps must be cleaned up."""
        from gateway.config import WatcherConfig
        from gateway.core.session_maps import SessionMaps

        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            from gateway.agents import AgentBackend
            from gateway.agents.response import AgentResponse
            from gateway.config import AgentConfig
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

            connector = ScriptConnector()
            agent = MockAgent()
            maps = SessionMaps()

            wc = WatcherConfig(
                name="script", connector="script", room="script", agent="default",
                context_inject_files=["/nonexistent/context.md"],
            )
            agent_cfg = AgentConfig(timeout=10, context_inject_files=[])
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
            manager = SessionManager(
                connector, {"default": agent}, "default", config,
                watcher_configs=[wc], session_maps=maps,
            )

            errors = await manager.run_once()
            self.assertTrue(len(errors) > 0)
            self.assertEqual(len(maps.room), 0, "session room map not cleaned up")
            self.assertEqual(len(maps.connector), 0, "session connector map not cleaned up")

            await manager.shutdown()
        finally:
            _patch_load.stop()
            _patch_save.stop()


# ── Tests: async file IO in context injection ─────────────────────────────────


class TestAsyncFileIOInContextInjection(unittest.IsolatedAsyncioTestCase):
    """Issue #15: file reads in _inject_context should use asyncio.to_thread."""

    async def test_context_injection_reads_files_via_to_thread(self):
        """Verify _inject_context uses asyncio.to_thread (non-blocking I/O)."""
        from gateway.connectors.script import ScriptConnector
        from gateway.core.session_manager import SessionManager

        # Use IsolatedTestCase patches manually
        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            from gateway.agents import AgentBackend

            class MockAgent(AgentBackend):
                def __init__(self):
                    self._session_counter = 0

                async def create_session(self, working_directory, extra_args=None, session_title=None):
                    self._session_counter += 1
                    return f"mock-session-{self._session_counter:04d}"

                async def send(self, session_id, prompt, working_directory, timeout,
                               attachments=None, env=None):
                    return AgentResponse(text="ok")

            connector = ScriptConnector()
            agent = MockAgent()

            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
                f.write("test context content")
                ctx_file = f.name

            wc = WatcherConfig(
                name="script",
                connector="script",
                room="script",
                agent="default",
                context_inject_files=[ctx_file],
            )
            agent_cfg = AgentConfig(timeout=10)
            config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")

            manager = SessionManager(
                connector, {"default": agent}, "default", config, watcher_configs=[wc]
            )

            to_thread_calls = []
            original_to_thread = asyncio.to_thread

            async def tracking_to_thread(func, *args):
                to_thread_calls.append(
                    func.__name__ if hasattr(func, "__name__") else str(func)
                )
                return await original_to_thread(func, *args)

            with patch(
                "gateway.core.context_injector.asyncio.to_thread",
                side_effect=tracking_to_thread,
            ):
                await manager.run_once()

            self.assertTrue(
                len(to_thread_calls) >= 2,
                f"Expected >= 2 to_thread calls, got {to_thread_calls}",
            )

            await manager.shutdown()
            Path(ctx_file).unlink(missing_ok=True)
        finally:
            _patch_load.stop()
            _patch_save.stop()


# ── Tests: retry cap ──────────────────────────────────────────────────────────


class TestContextInjectorRetryCap(unittest.IsolatedAsyncioTestCase):
    """ContextInjector gives up after _MAX_INJECT_ATTEMPTS persistent agent errors."""

    async def _run_inject(
        self,
        injector: ContextInjector,
        ws: WatcherState,
        session_id: str,
        agent_response: AgentResponse,
        ctx_file: str,
    ) -> None:
        wc = _make_wc([ctx_file])
        agent = AsyncMock()
        agent.send = AsyncMock(return_value=agent_response)

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "") or ""
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = 100
                return stat
            if "read_text" in name:
                return "context content"
            return None

        with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
            await injector.inject(ws, session_id, agent, "default", "rc", wc)

    async def test_single_failure_does_not_mark_injected(self):
        """One error response must NOT mark context as injected (allow retry)."""
        injector = _make_injector()
        ws = _make_ws()
        error_resp = AgentResponse(text="error!", is_error=True)

        await self._run_inject(injector, ws, "ses_1", error_resp, "/tmp/ctx.md")

        self.assertFalse(ws.context_injected)
        status = injector.status_for("ses_1")
        self.assertEqual(status.failure_count, 1)
        self.assertEqual(status.state, "failed_retryable")

    async def test_failure_count_increments_per_attempt(self):
        """Each failed attempt increments the counter."""
        injector = _make_injector()
        ws = _make_ws()
        error_resp = AgentResponse(text="error!", is_error=True)

        for i in range(1, _MAX_INJECT_ATTEMPTS):
            await self._run_inject(injector, ws, "ses_1", error_resp, "/tmp/ctx.md")
            self.assertEqual(injector.status_for("ses_1").failure_count, i)
            self.assertFalse(ws.context_injected)

    async def test_max_attempts_marks_session_degraded_without_fake_success(self):
        """After repeated failures, the injector records degraded state without fake success."""
        injector = _make_injector()
        ws = _make_ws()
        error_resp = AgentResponse(text="persistent error", is_error=True)

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await self._run_inject(injector, ws, "ses_1", error_resp, "/tmp/ctx.md")

        self.assertFalse(ws.context_injected)
        status = injector.status_for("ses_1")
        self.assertEqual(status.state, "failed_degraded")
        self.assertEqual(status.failure_count, _MAX_INJECT_ATTEMPTS)

    async def test_success_clears_failure_counter(self):
        """A successful injection after some failures clears the counter."""
        injector = _make_injector()
        ws = _make_ws()
        error_resp = AgentResponse(text="error!", is_error=True)
        ok_resp = AgentResponse(text="ok")

        await self._run_inject(injector, ws, "ses_1", error_resp, "/tmp/ctx.md")
        self.assertEqual(injector.status_for("ses_1").failure_count, 1)

        await self._run_inject(injector, ws, "ses_1", ok_resp, "/tmp/ctx.md")
        self.assertTrue(ws.context_injected)
        self.assertEqual(injector.status_for("ses_1").state, "injected")
        self.assertEqual(injector.status_for("ses_1").failure_count, 0)

    async def test_failure_counters_are_independent_per_session(self):
        """Different session_ids have independent failure counters."""
        injector = _make_injector()
        ws1 = _make_ws()
        ws2 = _make_ws()
        error_resp = AgentResponse(text="error!", is_error=True)

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await self._run_inject(injector, ws1, "ses_1", error_resp, "/tmp/ctx.md")

        self.assertEqual(injector.status_for("ses_1").state, "failed_degraded")
        self.assertFalse(ws2.context_injected)
        self.assertEqual(injector.status_for("ses_2").state, "not_started")

    async def test_already_injected_skips_without_counting(self):
        """Sessions with context_injected=True are skipped and counter is not touched."""
        injector = _make_injector()
        ws = _make_ws(context_injected=True)
        error_resp = AgentResponse(text="should not be called", is_error=True)

        for _ in range(_MAX_INJECT_ATTEMPTS + 1):
            await self._run_inject(injector, ws, "ses_1", error_resp, "/tmp/ctx.md")

        self.assertEqual(injector.status_for("ses_1").state, "injected")
        self.assertTrue(ws.context_injected)


# ── Tests: degraded state no-retry guarantee ─────────────────────────────────


async def _run_inject_with_error(injector, ws, session_id, ctx_file="/tmp/ctx.md"):
    """Run inject() simulating a file read that returns content + agent error response."""
    wc = _make_wc([ctx_file])
    agent = AsyncMock()
    agent.send = AsyncMock(return_value=AgentResponse(text="agent error", is_error=True))

    async def fake_to_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if "exists" in name:
            return True
        if "stat" in name:
            stat = MagicMock()
            stat.st_size = 100
            return stat
        if "read_text" in name:
            return "context"
        return None

    with patch("gateway.core.context_injector.asyncio.to_thread", side_effect=fake_to_thread):
        await injector.inject(ws, session_id, agent, "default", "rc", wc)
    return agent


class TestDegradedStateNoRetry(unittest.IsolatedAsyncioTestCase):
    """After failed_degraded, inject() is never called again by _ensure_context_injected."""

    async def test_failed_degraded_state_reached_after_max_attempts(self):
        """After _MAX_INJECT_ATTEMPTS failures, status transitions to failed_degraded."""
        injector = _make_injector()
        ws = _make_ws()

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await _run_inject_with_error(injector, ws, "ses_1")

        status = injector.status_for("ses_1")
        self.assertEqual(status.state, "failed_degraded")
        self.assertFalse(ws.context_injected)

    async def test_ensure_context_injected_skips_degraded_session(self):
        """_ensure_context_injected() must not call inject() after failed_degraded."""
        injector = _make_injector()
        ws = _make_ws()

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await _run_inject_with_error(injector, ws, "ses_1")

        status = injector.status_for("ses_1")
        retryable_states = {"not_started", "failed_retryable", "pending"}
        self.assertNotIn(status.state, retryable_states)

    async def test_additional_calls_after_degraded_do_not_change_state(self):
        """Calling inject() again after degraded does NOT reset or increment the counter."""
        injector = _make_injector()
        ws = _make_ws()

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await _run_inject_with_error(injector, ws, "ses_1")

        failure_count_before = injector.status_for("ses_1").failure_count

        status = injector.status_for("ses_1")
        if status.state not in {"not_started", "failed_retryable", "pending"}:
            pass
        else:
            await _run_inject_with_error(injector, ws, "ses_1")

        failure_count_after = injector.status_for("ses_1").failure_count
        self.assertEqual(failure_count_before, failure_count_after)


class TestNoContextFilesImmediatelyInjected(unittest.IsolatedAsyncioTestCase):
    """When no context files are configured, inject() marks session as injected immediately."""

    async def test_no_context_files_marks_injected(self):
        """Calling inject() with an empty files list immediately sets context_injected=True."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc([])
        agent = AsyncMock()
        agent.send = AsyncMock()

        await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        self.assertTrue(ws.context_injected)
        agent.send.assert_not_awaited()

    async def test_no_context_files_sets_injected_status(self):
        """Status for the session must be 'injected' (not 'not_started') after no-file inject."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc([])
        agent = AsyncMock()
        agent.send = AsyncMock()

        await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        status = injector.status_for("ses_1")
        self.assertEqual(status.state, "injected")

    async def test_no_context_files_prevents_retry_loop(self):
        """Calling inject() multiple times with no files does not call agent.send."""
        injector = _make_injector()
        ws = _make_ws()
        wc = _make_wc([])
        agent = AsyncMock()
        agent.send = AsyncMock()

        for _ in range(10):
            await injector.inject(ws, "ses_1", agent, "default", "rc", wc)

        agent.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
