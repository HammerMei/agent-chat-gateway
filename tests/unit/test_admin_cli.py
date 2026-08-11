"""Unit tests for gateway.admin.cli: argument parsing and the _run/_dispatch
exit-code and idempotency-notice behavior.

admin_factory/load_profiles/get_profile are patched so these tests exercise
only the CLI's own dispatch/error-handling logic, not real network calls.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from gateway.admin.cli import _configure_error_log, _run, build_parser, main
from gateway.admin.config import AdminConfigError


def _http_status_error(status_code: int, json_body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mm.example/api/v4/users")
    response = httpx.Response(status_code, request=request, json=json_body)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _args(argv: list[str]):
    return build_parser().parse_args(argv)


class TestBuildParser(unittest.TestCase):
    def test_create_user_parses_positional_and_optional_args(self):
        args = _args(["mm-lab", "create-user", "alice", "a@x.com", "pw", "--full-name", "Alice A"])
        self.assertEqual(args.profile, "mm-lab")
        self.assertEqual(args.command, "create-user")
        self.assertEqual(args.username, "alice")
        self.assertEqual(args.email, "a@x.com")
        self.assertEqual(args.password, "pw")
        self.assertEqual(args.full_name, "Alice A")

    def test_create_user_verified_defaults_to_false(self):
        args = _args(["mm-lab", "create-user", "alice", "a@x.com", "pw"])
        self.assertFalse(args.verified)

    def test_create_user_verified_flag(self):
        args = _args(["mm-lab", "create-user", "alice", "a@x.com", "pw", "--verified"])
        self.assertTrue(args.verified)

    def test_create_channel_private_flag(self):
        args = _args(["mm-lab", "create-channel", "secret", "--private"])
        self.assertTrue(args.private)

    def test_create_channel_defaults_to_public(self):
        args = _args(["mm-lab", "create-channel", "eng"])
        self.assertFalse(args.private)

    def test_config_flag_is_optional(self):
        args = _args(["--config", "/tmp/x.yaml", "mm-lab", "delete-user", "alice"])
        self.assertEqual(args.config, "/tmp/x.yaml")

    def test_log_file_defaults_to_msg_admin_log(self):
        args = _args(["mm-lab", "delete-user", "alice"])
        self.assertEqual(args.log_file, "msg-admin.log")

    def test_log_file_flag_overrides_default(self):
        args = _args(["--log-file", "/tmp/custom.log", "mm-lab", "delete-user", "alice"])
        self.assertEqual(args.log_file, "/tmp/custom.log")


class TestRunDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # _run() always calls _configure_error_log(args.log_file), which
        # defaults to a real, cwd-relative "msg-admin.log" — none of these
        # tests care about that concern (it's covered by TestConfigureErrorLog
        # and TestHttpStatusErrorHandling below), so stub it out to avoid
        # every test in this class writing a stray file to the repo.
        patcher = patch("gateway.admin.cli._configure_error_log")
        self.addCleanup(patcher.stop)
        patcher.start()

    async def test_unknown_profile_returns_1(self):
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", side_effect=AdminConfigError("nope")):
            args = _args(["ghost", "delete-user", "alice"])
            self.assertEqual(await _run(args), 1)

    async def test_create_user_success_returns_0(self):
        mock_admin = AsyncMock()
        mock_admin.create_user = AsyncMock(
            return_value=AdminUser(id="u1", username="alice", email="a@x.com")
        )
        with patch("gateway.admin.cli.load_profiles", return_value={"p": object()}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.connect.assert_awaited_once()
        mock_admin.close.assert_awaited_once()
        mock_admin.create_user.assert_awaited_once_with(
            "alice", "a@x.com", "pw", full_name=None, verified=False
        )

    async def test_create_user_already_exists_is_not_an_error(self):
        mock_admin = AsyncMock()
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        mock_admin.create_user = AsyncMock(side_effect=UserAlreadyExistsError("alice", existing=existing))
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        # Idempotent-by-default: already-exists is a note, not a failure.
        self.assertEqual(code, 0)
        mock_admin.close.assert_awaited_once()

    async def test_create_channel_already_exists_is_not_an_error(self):
        mock_admin = AsyncMock()
        existing = AdminChannel(id="c1", name="eng", is_private=False)
        mock_admin.create_channel = AsyncMock(
            side_effect=ChannelAlreadyExistsError("eng", existing=existing)
        )
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)

    async def test_not_found_error_returns_1_and_still_closes(self):
        mock_admin = AsyncMock()
        mock_admin.delete_user = AsyncMock(side_effect=UserNotFoundError("no such user"))
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-user", "ghost"])
            code = await _run(args)

        self.assertEqual(code, 1)
        mock_admin.close.assert_awaited_once()

    async def test_create_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        mock_admin.create_channel = AsyncMock(
            return_value=AdminChannel(id="c1", name="eng", is_private=False)
        )
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.create_channel.assert_awaited_once_with("eng", is_private=False)

    async def test_add_to_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "add-to-channel", "alice", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.add_user_to_channel.assert_awaited_once_with("alice", "eng")

    async def test_delete_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.delete_channel.assert_awaited_once_with("eng")

    async def test_delete_user_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-user", "alice"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.delete_user.assert_awaited_once_with("alice")

    async def test_admin_factory_error_returns_1_without_connecting(self):
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", side_effect=AdminConfigError("bad profile")):
            args = _args(["p", "delete-user", "ghost"])
            code = await _run(args)

        self.assertEqual(code, 1)


class TestConfigureErrorLog(unittest.TestCase):
    def setUp(self):
        self.error_logger = logging.getLogger("agent-chat-gateway.admin.errors")
        self.umbrella_logger = logging.getLogger("agent-chat-gateway")
        self._orig_error_handlers = list(self.error_logger.handlers)
        self._orig_error_propagate = self.error_logger.propagate
        self._orig_umbrella_handlers = list(self.umbrella_logger.handlers)
        self.error_logger.handlers = []
        self.umbrella_logger.handlers = []
        self.addCleanup(setattr, self.error_logger, "handlers", self._orig_error_handlers)
        self.addCleanup(setattr, self.error_logger, "propagate", self._orig_error_propagate)
        self.addCleanup(setattr, self.umbrella_logger, "handlers", self._orig_umbrella_handlers)

    def test_attaches_a_file_handler_to_error_logger(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.assertEqual(len(self.error_logger.handlers), 1)
            self.assertIsInstance(self.error_logger.handlers[0], logging.FileHandler)
            self.assertEqual(self.error_logger.level, logging.ERROR)

    def test_disables_propagation_on_error_logger(self):
        # Otherwise log_error_response()'s explicit call would ALSO be
        # handled by the umbrella handler below (same file) — one call,
        # two lines written.
        with tempfile.TemporaryDirectory() as d:
            _configure_error_log(os.path.join(d, "custom.log"))

            self.assertFalse(self.error_logger.propagate)

    def test_attaches_a_warning_level_handler_to_umbrella_logger(self):
        # This is the actual fix: RocketChatREST/MattermostREST's own
        # logger.error() calls (on loggers named
        # "agent-chat-gateway.connectors.<platform>.rest") would otherwise
        # find no handler anywhere in their hierarchy and fall through to
        # Python's stderr-printing "handler of last resort".
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.assertEqual(len(self.umbrella_logger.handlers), 1)
            handler = self.umbrella_logger.handlers[0]
            self.assertIsInstance(handler, logging.FileHandler)
            self.assertEqual(handler.level, logging.WARNING)

    def test_idempotent_does_not_duplicate_either_handler_for_same_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)
            _configure_error_log(path)

            self.assertEqual(len(self.error_logger.handlers), 1)
            self.assertEqual(len(self.umbrella_logger.handlers), 1)

    def test_error_logged_via_error_logger_is_written_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.error_logger.error("boom")
            for handler in self.error_logger.handlers + self.umbrella_logger.handlers:
                handler.flush()

            with open(path) as f:
                lines = [line for line in f.read().splitlines() if "boom" in line]
            self.assertEqual(len(lines), 1)

    def test_rest_client_logger_error_reaches_the_file(self):
        # Simulates what MattermostREST/RocketChatREST's shared _request()
        # does on a non-2xx response — a logger under the
        # "agent-chat-gateway.connectors.*" namespace, which has no handler
        # of its own and relies on propagation up to the umbrella logger.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            rest_logger = logging.getLogger("agent-chat-gateway.connectors.mattermost.rest")
            rest_logger.error("Mattermost API error 400 for POST users — body: {...}")
            for handler in self.umbrella_logger.handlers:
                handler.flush()

            with open(path) as f:
                content = f.read()
            self.assertIn("Mattermost API error 400", content)


class TestHttpStatusErrorHandling(unittest.IsolatedAsyncioTestCase):
    async def test_prints_friendly_message_and_logs_full_body(self):
        mock_admin = AsyncMock()
        mock_admin.create_user = AsyncMock(
            side_effect=_http_status_error(
                400,
                {
                    "id": "app.user.save.email_exists.app_error",
                    "message": "An account with that email already exists.",
                    "request_id": "req-123",
                    "status_code": 400,
                },
            )
        )
        mock_logger = MagicMock()
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             patch("gateway.admin.cli._configure_error_log"), \
             patch("gateway.admin.cli._error_logger", mock_logger), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 1)
        mock_logger.error.assert_called_once()
        mock_admin.close.assert_awaited_once()
        output = stderr.getvalue()
        self.assertIn("An account with that email already exists.", output)
        self.assertNotIn("Client error", output)  # httpx's generic message must not leak through
        self.assertIn(args.log_file, output)


class TestUnwritableLogFile(unittest.IsolatedAsyncioTestCase):
    async def test_unwritable_log_file_returns_1_with_clean_message_not_a_traceback(self):
        # logging.FileHandler opens the file immediately — simulate a
        # read-only directory / bad path by having _configure_error_log
        # raise the same way FileHandler would.
        with patch(
            "gateway.admin.cli._configure_error_log",
            side_effect=OSError("Permission denied"),
        ):
            args = _args(["--log-file", "/no/such/dir/x.log", "p", "delete-user", "alice"])
            code = await _run(args)

        self.assertEqual(code, 1)

    async def test_unwritable_log_file_does_not_reach_admin_factory(self):
        # The failure happens before any profile/admin setup — nothing
        # downstream should be touched.
        with patch(
            "gateway.admin.cli._configure_error_log", side_effect=OSError("Permission denied")
        ), patch("gateway.admin.cli.admin_factory") as mock_factory:
            args = _args(["--log-file", "/no/such/dir/x.log", "p", "delete-user", "alice"])
            await _run(args)

        mock_factory.assert_not_called()


class TestMain(unittest.TestCase):
    def test_main_exits_with_run_result_code(self):
        with patch("gateway.admin.cli._run", new=AsyncMock(return_value=7)), \
             patch("sys.argv", ["msg-admin", "p", "delete-user", "alice"]), \
             self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 7)


if __name__ == "__main__":
    unittest.main()
