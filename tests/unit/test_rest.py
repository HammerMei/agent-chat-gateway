"""Unit tests for RocketChatREST.

All network I/O is mocked via httpx.MockTransport / respx-style patches so
no real server is required.  Each test targets a single method and verifies:
  - Happy path (correct return value / side effects)
  - Error paths (4xx/5xx, auth expiry, not-found, missing fields)
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.connectors.rocketchat.rest import RocketChatREST, RoomNotFoundError

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    json_body: dict | None = None,
    text_body: str = "",
) -> httpx.Response:
    """Build a minimal httpx.Response without a real transport."""
    body = (
        json.dumps(json_body or {}).encode()
        if json_body is not None
        else text_body.encode()
    )
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://example.com"),
    )


def _make_rest() -> RocketChatREST:
    return RocketChatREST("http://chat.example.com")


# ── __repr__ ──────────────────────────────────────────────────────────────────


class TestRepr(unittest.TestCase):
    def test_repr_masks_password(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "s3cr3t"
        r = repr(rest)
        self.assertIn("bot", r)
        self.assertNotIn("s3cr3t", r)
        self.assertIn("***", r)

    def test_repr_no_credentials(self):
        rest = _make_rest()
        r = repr(rest)
        self.assertIn("None", r)


# ── close ─────────────────────────────────────────────────────────────────────


class TestClose(unittest.IsolatedAsyncioTestCase):
    async def test_close_calls_aclose_on_both_clients(self):
        rest = _make_rest()
        rest._client.aclose = AsyncMock()
        rest._download_client.aclose = AsyncMock()
        await rest.close()
        rest._client.aclose.assert_called_once()
        rest._download_client.aclose.assert_called_once()


# ── login ─────────────────────────────────────────────────────────────────────


class TestLogin(unittest.IsolatedAsyncioTestCase):
    async def test_login_success_stores_credentials(self):
        rest = _make_rest()
        ok_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "tok123", "userId": "uid456"},
            },
        )
        rest._client.post = AsyncMock(return_value=ok_resp)

        await rest.login("bot", "pass")

        self.assertEqual(rest.auth_token, "tok123")
        self.assertEqual(rest.user_id, "uid456")
        self.assertEqual(rest.bot_username, "bot")
        self.assertEqual(rest._username, "bot")
        self.assertEqual(rest._password, "pass")

    async def test_login_status_not_success_raises(self):
        rest = _make_rest()
        bad_resp = _make_response(200, {"status": "error", "message": "bad creds"})
        rest._client.post = AsyncMock(return_value=bad_resp)

        with self.assertRaises(RuntimeError, msg="Login failed"):
            await rest.login("bot", "wrongpass")

    async def test_login_http_error_raises(self):
        rest = _make_rest()
        err_resp = _make_response(401, {"message": "Unauthorized"})
        rest._client.post = AsyncMock(return_value=err_resp)

        with self.assertRaises(httpx.HTTPStatusError):
            await rest.login("bot", "bad")


# ── _request ──────────────────────────────────────────────────────────────────


class TestRequest(unittest.IsolatedAsyncioTestCase):
    async def test_request_returns_json_on_success(self):
        rest = _make_rest()
        rest.auth_token = "t"
        rest.user_id = "u"
        resp = _make_response(200, {"success": True, "data": "value"})
        rest._client.request = AsyncMock(return_value=resp)

        result = await rest._request("GET", "some.endpoint")
        self.assertEqual(result["success"], True)

    async def test_request_401_triggers_relogin_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        ok_resp = _make_response(200, {"success": True})

        # First call → 401, second call (retry) → 200
        rest._client.request = AsyncMock(side_effect=[unauthorized, ok_resp])

        # login() is called during re-auth; mock it
        login_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "new_tok", "userId": "uid"},
            },
        )
        rest._client.post = AsyncMock(return_value=login_resp)

        result = await rest._request("GET", "some.endpoint")
        self.assertEqual(result["success"], True)
        self.assertEqual(rest.auth_token, "new_tok")

    async def test_request_401_with_no_password_raises_runtime_error(self):
        """Q3: 401 re-login path must raise RuntimeError when _password is None.

        Previously, _password was typed str | None but passed to login() without
        a None guard — this would produce a confusing TypeError deep in httpx.
        Now it raises a clear RuntimeError.
        """
        rest = _make_rest()
        rest._username = "bot"
        rest._password = None  # simulate uninitialized state
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        rest._client.request = AsyncMock(return_value=unauthorized)

        with self.assertRaises(RuntimeError) as ctx:
            await rest._request("GET", "some.endpoint")
        self.assertIn("Cannot re-login", str(ctx.exception))

    async def test_request_401_with_no_username_skips_relogin(self):
        """Q3 (related): When _username is None, the 401 re-login path is
        skipped entirely (outer guard 'and self._username' is falsy) and the
        request raises HTTPStatusError directly — no confusing TypeError."""
        rest = _make_rest()
        rest._username = None  # outer guard 'and self._username' will be falsy
        rest._password = "pw"
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        rest._client.request = AsyncMock(return_value=unauthorized)

        # Re-login is skipped; falls through to raise_for_status() → HTTPStatusError
        with self.assertRaises(httpx.HTTPStatusError):
            await rest._request("GET", "some.endpoint")

    async def test_request_non_success_raises_http_status_error(self):
        rest = _make_rest()
        resp = _make_response(500, {"error": "Internal Server Error"})
        rest._client.request = AsyncMock(return_value=resp)

        with self.assertRaises(httpx.HTTPStatusError):
            await rest._request("GET", "some.endpoint")

    async def test_request_passes_params_and_json(self):
        rest = _make_rest()
        resp = _make_response(200, {"ok": True})
        rest._client.request = AsyncMock(return_value=resp)

        await rest._request(
            "POST", "chat.postMessage", json_data={"text": "hi"}, params={"foo": "bar"}
        )
        rest._client.request.assert_called_once()
        _, kwargs = rest._client.request.call_args
        self.assertEqual(kwargs["json"], {"text": "hi"})
        self.assertEqual(kwargs["params"], {"foo": "bar"})


# ── post_message ──────────────────────────────────────────────────────────────


class TestPostMessage(unittest.IsolatedAsyncioTestCase):
    async def _patched_rest(self, response_body: dict) -> RocketChatREST:
        rest = _make_rest()
        rest._request = AsyncMock(return_value=response_body)
        return rest

    async def test_post_message_no_thread(self):
        rest = await self._patched_rest({"success": True})
        await rest.post_message("general", "Hello!")
        rest._request.assert_called_once_with(
            "POST",
            "chat.postMessage",
            json_data={"channel": "general", "text": "Hello!"},
        )

    async def test_post_message_with_thread(self):
        rest = await self._patched_rest({"success": True})
        await rest.post_message("ROOM123", "reply", tmid="THREAD456")
        rest._request.assert_called_once_with(
            "POST",
            "chat.postMessage",
            json_data={"roomId": "ROOM123", "text": "reply", "tmid": "THREAD456"},
        )

    async def test_post_message_failure_raises(self):
        rest = await self._patched_rest({"success": False, "error": "not_in_room"})
        with self.assertRaises(RuntimeError, msg="not_in_room"):
            await rest.post_message("general", "hi")


# ── download_file ─────────────────────────────────────────────────────────────


class TestDownloadFile(unittest.IsolatedAsyncioTestCase):
    async def test_download_writes_bytes_to_disk(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        # Build a fake streaming response
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        async def _fake_aiter_bytes():
            yield b"chunk1"
            yield b"chunk2"

        fake_resp.aiter_bytes = _fake_aiter_bytes
        rest._download_client.stream = MagicMock(return_value=fake_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "file.bin")
            await rest.download_file("/file-upload/abc/test.bin", dest)
            self.assertEqual(Path(dest).read_bytes(), b"chunk1chunk2")

    async def test_download_http_error_raises(self):
        rest = _make_rest()

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "403",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
            )
        )
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)
        rest._download_client.stream = MagicMock(return_value=fake_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(httpx.HTTPStatusError):
                await rest.download_file(
                    "/file-upload/abc/file.bin", str(Path(tmpdir) / "out.bin")
                )

    async def test_download_401_relogs_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old"
        rest.user_id = "uid"

        unauthorized = MagicMock()
        unauthorized.status_code = 401
        unauthorized.__aenter__ = AsyncMock(return_value=unauthorized)
        unauthorized.__aexit__ = AsyncMock(return_value=False)
        unauthorized.aiter_bytes = AsyncMock()

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        async def _fake_aiter_bytes():
            yield b"retry-ok"

        ok_resp.aiter_bytes = _fake_aiter_bytes

        rest._download_client.stream = MagicMock(side_effect=[unauthorized, ok_resp])
        login_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "new_tok", "userId": "uid"},
            },
        )
        rest._client.post = AsyncMock(return_value=login_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "file.bin")
            await rest.download_file("/file-upload/abc/test.bin", dest)
            self.assertEqual(Path(dest).read_bytes(), b"retry-ok")
            self.assertEqual(rest.auth_token, "new_tok")
            self.assertEqual(rest._download_client.stream.call_count, 2)


# ── upload_file ───────────────────────────────────────────────────────────────


class TestUploadFile(unittest.IsolatedAsyncioTestCase):
    async def test_upload_missing_file_raises(self):
        rest = _make_rest()
        with self.assertRaises(FileNotFoundError):
            await rest.upload_file("ROOM1", "/nonexistent/path/file.txt")

    async def test_upload_success(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "report.txt"
            fpath.write_bytes(b"hello world")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath), caption="my file")
            rest._download_client.post.assert_called_once()
            _, kwargs = rest._download_client.post.call_args
            self.assertEqual(kwargs["data"], {"msg": "my file"})

    async def test_upload_no_caption_omits_msg(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "img.png"
            fpath.write_bytes(b"\x89PNG")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath))
            _, kwargs = rest._download_client.post.call_args
            self.assertEqual(kwargs["data"], {})

    async def test_upload_api_failure_raises(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "doc.pdf"
            fpath.write_bytes(b"%PDF")

            fail_resp = _make_response(200, {"success": False, "error": "too_large"})
            rest._download_client.post = AsyncMock(return_value=fail_resp)

            with self.assertRaises(RuntimeError, msg="too_large"):
                await rest.upload_file("ROOM1", str(fpath))

    async def test_upload_401_relogins_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old_tok"
        rest.user_id = "uid"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "file.txt"
            fpath.write_bytes(b"data")

            unauth = _make_response(401, {"message": "Unauthorized"})
            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(side_effect=[unauth, ok_resp])

            login_resp = _make_response(
                200,
                {
                    "status": "success",
                    "data": {"authToken": "new_tok", "userId": "uid"},
                },
            )
            rest._client.post = AsyncMock(return_value=login_resp)

            await rest.upload_file("ROOM1", str(fpath))
            self.assertEqual(rest.auth_token, "new_tok")
            self.assertEqual(rest._download_client.post.call_count, 2)

    async def test_upload_unknown_mime_falls_back_to_octet_stream(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "weirdfile.xyz123"
            fpath.write_bytes(b"binary data")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath))
            _, kwargs = rest._download_client.post.call_args
            # mime type is embedded in the files tuple: (name, bytes, mime)
            file_tuple = kwargs["files"]["file"]
            self.assertEqual(file_tuple[2], "application/octet-stream")


# ── resolve_room ──────────────────────────────────────────────────────────────


class TestResolveRoom(unittest.IsolatedAsyncioTestCase):
    # ── DM (@username) ────────────────────────────────────────────────────────

    async def test_resolve_dm_success(self):
        rest = _make_rest()
        rest._request = AsyncMock(
            return_value={
                "success": True,
                "room": {"_id": "DM_ROOM_ID"},
            }
        )
        result = await rest.resolve_room("@alice")
        self.assertEqual(result["_id"], "DM_ROOM_ID")
        self.assertEqual(result["type"], "dm")
        self.assertEqual(result["name"], "@alice")

    async def test_resolve_dm_http_error_raises_runtime(self):
        rest = _make_rest()
        err_response = _make_response(404)
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404",
                request=err_response.request,
                response=err_response,
            )
        )
        with self.assertRaises(RuntimeError, msg="Failed to open DM"):
            await rest.resolve_room("@ghost")

    async def test_resolve_dm_no_room_in_response_raises_not_found(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": False})
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("@nobody")

    # ── Public channel ────────────────────────────────────────────────────────

    async def test_resolve_public_channel_success(self):
        rest = _make_rest()
        rest._request = AsyncMock(
            return_value={
                "success": True,
                "channel": {"_id": "CH_ID", "name": "general"},
            }
        )
        result = await rest.resolve_room("general")
        self.assertEqual(result["_id"], "CH_ID")
        self.assertEqual(result["type"], "channel")
        self.assertEqual(result["name"], "general")

    async def test_resolve_channel_404_falls_through_to_group(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        # channels.info → 404 (channel not found), groups.info → success
        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP_ID", "name": "secret"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("secret")
        self.assertEqual(result["_id"], "GRP_ID")
        self.assertEqual(result["type"], "group")

    async def test_resolve_channel_400_falls_through_to_group(self):
        rest = _make_rest()
        err_resp = _make_response(400)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "400", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP_ID", "name": "priv"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("priv")
        self.assertEqual(result["type"], "group")

    async def test_resolve_channel_500_re_raises(self):
        rest = _make_rest()
        err_resp = _make_response(500)

        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=err_resp.request, response=err_resp
            )
        )
        with self.assertRaises(httpx.HTTPStatusError):
            await rest.resolve_room("general")

    # ── Private group ─────────────────────────────────────────────────────────

    async def test_resolve_private_group_success(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP99", "name": "private-stuff"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("private-stuff")
        self.assertEqual(result["_id"], "GRP99")
        self.assertEqual(result["type"], "group")
        self.assertEqual(result["name"], "private-stuff")

    async def test_resolve_group_500_re_raises(self):
        rest = _make_rest()
        ch_err = _make_response(404)
        grp_err = _make_response(500)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=ch_err.request, response=ch_err
                )
            raise httpx.HTTPStatusError(
                "500", request=grp_err.request, response=grp_err
            )

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(httpx.HTTPStatusError):
            await rest.resolve_room("some-room")

    async def test_resolve_not_found_raises_room_not_found(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        # Both channels.info and groups.info return 404
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=err_resp.request, response=err_resp
            )
        )
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("nowhere")

    async def test_resolve_channel_success_field_falls_through(self):
        """success=True but no 'channel' key → falls through to groups lookup."""
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                # RC returns 200 success=True but no channel key (edge case)
                return {"success": True}
            raise httpx.HTTPStatusError(
                "404", request=err_resp.request, response=err_resp
            )

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("weird-room")

    async def test_resolve_group_success_field_falls_through_to_not_found(self):
        """groups.info returns success=True but no 'group' key → RoomNotFoundError."""
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True}  # no 'group' key

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("weird-group")


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestReloginLock(unittest.IsolatedAsyncioTestCase):
    """Concurrent 401 responses must only trigger one login() call."""

    def _make_rest(self):
        r = RocketChatREST("http://example.com")
        r._username = "bot"
        r._password = "secret"
        r.auth_token = "old_tok"
        r.user_id = "uid"
        r._client = MagicMock()
        return r

    async def test_relogin_lock_exists(self):
        """RocketChatREST must initialize _relogin_lock as asyncio.Lock."""
        rest = RocketChatREST("http://x")
        await rest.close()
        self.assertIsInstance(rest._relogin_lock, asyncio.Lock)

    async def test_concurrent_401_calls_login_once(self):
        """Two simultaneous 401 responses must only invoke login() once."""
        rest = self._make_rest()
        login_count = 0

        async def fake_login(username, password):
            nonlocal login_count
            login_count += 1
            rest.auth_token = "new_tok"
            rest.user_id = "uid"
            rest.bot_username = username
            rest._username = username
            rest._password = password

        def _make_401():
            r = MagicMock()
            r.status_code = 401
            r.is_success = False
            r.raise_for_status.side_effect = Exception("401")
            return r

        def _make_200():
            r = MagicMock()
            r.status_code = 200
            r.is_success = True
            r.raise_for_status = MagicMock()
            r.json.return_value = {"success": True}
            return r

        call_count = 0

        async def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_401()
            return _make_200()

        rest._client.request = fake_request
        rest.login = fake_login

        await asyncio.gather(
            rest._request("GET", "endpoint"),
            rest._request("GET", "endpoint"),
            return_exceptions=True,
        )

        self.assertEqual(login_count, 1, f"login() should be called once, got {login_count}")

    async def test_skip_relogin_when_token_already_refreshed(self):
        """If token already refreshed while waiting, skip re-login."""
        rest = self._make_rest()
        rest.auth_token = "old_tok"
        login_count = 0

        async def fake_login(username, password):
            nonlocal login_count
            login_count += 1
            rest.auth_token = "refreshed"

        r_401 = MagicMock()
        r_401.status_code = 401
        r_401.is_success = False
        r_401.raise_for_status.side_effect = Exception("401")

        r_200 = MagicMock()
        r_200.status_code = 200
        r_200.is_success = True
        r_200.raise_for_status = MagicMock()
        r_200.json.return_value = {"ok": True}

        class TokenChangingLock:
            async def __aenter__(self_inner):
                rest.auth_token = "refreshed"
                return self_inner
            async def __aexit__(self_inner, *_):
                pass

        rest._relogin_lock = TokenChangingLock()
        rest._client.request = AsyncMock(side_effect=[r_401, r_200])
        rest.login = fake_login

        await rest._request("GET", "ep")
        self.assertEqual(login_count, 0, "login() must not be called when token already refreshed")


# ── Appended from test_round8_fixes.py ────────────────────────────────────────


class TestDownloadFileReauthNotNested(unittest.IsolatedAsyncioTestCase):
    """Second streaming request must be opened AFTER the first context manager exits."""

    async def test_reauth_request_opened_after_first_context_closed(self):
        """Verify that on 401 the code exits the first context before retrying."""
        rest = RocketChatREST("http://example.com")
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "tok"
        rest.user_id = "uid"

        open_order: list[str] = []
        close_order: list[str] = []

        class FakeStream:
            def __init__(self, name: str, status: int, body: bytes = b""):
                self._name = name
                self._status = status
                self._body = body

            async def __aenter__(self):
                open_order.append(self._name)
                self.status_code = self._status
                return self

            async def __aexit__(self, *_):
                close_order.append(self._name)

            def raise_for_status(self):
                if self._status >= 400:
                    raise Exception(f"HTTP {self._status}")

            async def aiter_bytes(self):
                yield self._body

        call_count = 0

        def fake_stream(method, url, headers=None, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeStream("first", 401)
            return FakeStream("second", 200, b"data")

        rest._download_client = MagicMock()
        rest._download_client.stream = fake_stream

        async def fake_login(u, p):
            rest.auth_token = "new_tok"
            rest.user_id = "uid"
            rest.bot_username = u
            rest._username = u
            rest._password = p

        rest.login = fake_login

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "out.bin")
            await rest.download_file("/file/path", dest)

        first_close_idx = close_order.index("first")
        second_open_idx = open_order.index("second")
        self.assertLess(
            first_close_idx,
            second_open_idx,
            f"First context must close before second opens. "
            f"open_order={open_order}, close_order={close_order}",
        )

    async def test_successful_download_no_reauth(self):
        """A 200 response must not trigger re-auth."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"

        login_called = []

        async def fake_login(u, p):
            login_called.append(True)

        rest.login = fake_login

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"hello"

        rest._download_client = MagicMock()
        rest._download_client.stream = lambda method, url, **kw: FakeStream()

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "out.bin")
            await rest.download_file("/file/path", dest)

        self.assertEqual(login_called, [], "login() must not be called on 200")


class TestUploadFileNonBlocking(unittest.IsolatedAsyncioTestCase):
    """upload_file must use asyncio.to_thread for file reading."""

    async def test_upload_uses_to_thread_for_file_read(self):
        """path.read_bytes must be called via asyncio.to_thread, not directly."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"

        to_thread_fns: list = []
        original_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            to_thread_fns.append(fn)
            return await original_to_thread(fn, *args, **kwargs)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}
        rest._download_client = MagicMock()
        rest._download_client.post = AsyncMock(return_value=mock_response)

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "test.txt")
            Path(fpath).write_bytes(b"hello world")

            with patch("gateway.connectors.rocketchat.rest.asyncio.to_thread", side_effect=spy_to_thread):
                await rest.upload_file("room1", fpath)

        read_bytes_calls = [fn for fn in to_thread_fns if getattr(fn, "__name__", "") == "read_bytes"]
        self.assertGreaterEqual(
            len(read_bytes_calls),
            1,
            "path.read_bytes must be called via asyncio.to_thread",
        )


# ── Appended from test_round9_fixes.py ────────────────────────────────────────


class TestDownloadFileUniqueTmpPath(unittest.IsolatedAsyncioTestCase):
    """Concurrent downloads for the same destination must use distinct tmp paths."""

    async def test_concurrent_downloads_have_distinct_tmp_paths(self):
        """Each download call must generate a unique tmp_path suffix."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"

        import secrets as secrets_mod

        generated_tokens: list[str] = []
        original_token_hex = secrets_mod.token_hex

        def capture_token_hex(n):
            token = original_token_hex(n)
            generated_tokens.append(token)
            return token

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"data"

        rest._download_client = MagicMock()
        rest._download_client.stream = lambda method, url, **kw: FakeStream()

        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "file.bin")

            with patch("gateway.connectors.rocketchat.rest.secrets.token_hex", side_effect=capture_token_hex):
                await asyncio.gather(
                    rest.download_file("/file/path", dest),
                    rest.download_file("/file/path", dest),
                )

        self.assertGreaterEqual(len(generated_tokens), 2, "Expected at least 2 token_hex calls")
        self.assertEqual(
            len(set(generated_tokens)),
            len(generated_tokens),
            f"tmp_path tokens must be unique across concurrent downloads; got {generated_tokens}",
        )
