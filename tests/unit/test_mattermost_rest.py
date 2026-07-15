"""Unit tests for MattermostREST.

All network I/O is mocked via httpx.Response objects constructed in-memory —
same approach as test_rest.py (RC). Each test targets a single method and
verifies the happy path plus the platform-specific quirks confirmed against
a live Mattermost 11.7.0 server during implementation:
  - login() reads the session token from the 'Token' RESPONSE HEADER, not
    the JSON body (unlike RC).
  - resolve_team() uses GET /users/me/teams (not /teams/name/{name}, which
    403s for non-admin bot accounts even when they ARE team members).
  - get_room_history()'s before_ts/after_ts are epoch-ms strings, not ISO.
  - Token-mode auth does not attempt re-login on 401 (nothing to log back
    in with); username/password mode does.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

import httpx

from gateway.connectors.mattermost.rest import (
    MattermostREST,
    RoomNotFoundError,
    iso_to_epoch_ms_str,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    json_body=None,
    headers: dict | None = None,
) -> httpx.Response:
    body = json.dumps(json_body).encode() if json_body is not None else b""
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers=headers or {"content-type": "application/json"},
        request=httpx.Request("GET", "http://example.com"),
    )


def _make_rest(**kwargs) -> MattermostREST:
    return MattermostREST("http://mm.example.com", **kwargs)


# ── authenticate ──────────────────────────────────────────────────────────────


class TestAuthenticate(unittest.IsolatedAsyncioTestCase):
    async def test_token_mode_does_not_call_login(self):
        rest = _make_rest(token="abc")
        rest.login = AsyncMock()
        await rest.authenticate()
        rest.login.assert_not_called()
        self.assertEqual(rest._token, "abc")

    async def test_password_mode_calls_login(self):
        rest = _make_rest(username="bot", password="pw")
        rest.login = AsyncMock()
        await rest.authenticate()
        rest.login.assert_called_once_with("bot", "pw")

    async def test_no_credentials_raises(self):
        rest = _make_rest()
        with self.assertRaises(RuntimeError):
            await rest.authenticate()


# ── login ─────────────────────────────────────────────────────────────────────


class TestLogin(unittest.IsolatedAsyncioTestCase):
    async def test_login_reads_token_from_header(self):
        rest = _make_rest()
        resp = _make_response(200, {"id": "uid"}, headers={"Token": "sess-tok-123"})
        rest._client.post = AsyncMock(return_value=resp)

        await rest.login("bot", "pw")

        self.assertEqual(rest._token, "sess-tok-123")
        self.assertEqual(rest._username, "bot")
        self.assertEqual(rest._password, "pw")

    async def test_login_missing_token_header_raises(self):
        rest = _make_rest()
        resp = _make_response(200, {"id": "uid"})  # no Token header
        rest._client.post = AsyncMock(return_value=resp)

        with self.assertRaises(RuntimeError):
            await rest.login("bot", "pw")


# ── get_me ────────────────────────────────────────────────────────────────────


class TestGetMe(unittest.IsolatedAsyncioTestCase):
    async def test_stores_bot_identity(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value={"id": "bot-id-1", "username": "hammer.mei"})

        result = await rest.get_me()

        self.assertEqual(rest.bot_user_id, "bot-id-1")
        self.assertEqual(rest.bot_username, "hammer.mei")
        self.assertEqual(result["id"], "bot-id-1")


# ── resolve_team ──────────────────────────────────────────────────────────────


class TestResolveTeam(unittest.IsolatedAsyncioTestCase):
    async def test_finds_team_by_name(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=[
            {"id": "team-1", "name": "bugfamily"},
            {"id": "team-2", "name": "other"},
        ])

        team_id = await rest.resolve_team("bugfamily")

        self.assertEqual(team_id, "team-1")
        self.assertEqual(rest.team_id, "team-1")
        rest._request.assert_called_once_with("GET", "users/me/teams")

    async def test_finds_team_by_id(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=[{"id": "team-1", "name": "bugfamily"}])

        team_id = await rest.resolve_team("team-1")
        self.assertEqual(team_id, "team-1")

    async def test_team_not_found_raises_room_not_found(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=[])

        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_team("nonexistent")

    async def test_uses_users_me_teams_not_teams_by_name(self):
        """Regression guard: /teams/name/{name} 403s for non-admin bots even
        when they ARE team members — confirmed against a live server."""
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=[{"id": "team-1", "name": "t"}])
        await rest.resolve_team("t")
        called_endpoint = rest._request.call_args[0][1]
        self.assertEqual(called_endpoint, "users/me/teams")


# ── resolve_username ──────────────────────────────────────────────────────────


class TestResolveUsername(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_and_caches(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value={"id": "u1", "username": "alice"})

        first = await rest.resolve_username("u1")
        second = await rest.resolve_username("u1")

        self.assertEqual(first, "alice")
        self.assertEqual(second, "alice")
        rest._request.assert_called_once()  # second call hit the cache


# ── post_message ──────────────────────────────────────────────────────────────


class TestPostMessage(unittest.IsolatedAsyncioTestCase):
    async def test_basic_post(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value={"id": "post1"})

        await rest.post_message("chan1", "hello")

        rest._request.assert_called_once_with(
            "POST", "posts", json_data={"channel_id": "chan1", "message": "hello"}
        )

    async def test_post_with_root_id_and_file_ids(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value={"id": "post2"})

        await rest.post_message("chan1", "hello", root_id="root1", file_ids=["f1", "f2"])

        rest._request.assert_called_once_with(
            "POST", "posts",
            json_data={
                "channel_id": "chan1", "message": "hello",
                "root_id": "root1", "file_ids": ["f1", "f2"],
            },
        )


# ── get_room_history ──────────────────────────────────────────────────────────


class TestGetRoomHistory(unittest.IsolatedAsyncioTestCase):
    def _sample_response(self):
        return {
            "order": ["p3", "p1", "p2"],  # newest-first from the API
            "posts": {
                "p1": {"id": "p1", "create_at": 100, "message": "first", "type": ""},
                "p2": {"id": "p2", "create_at": 200, "message": "", "type": ""},  # empty -> excluded
                "p3": {"id": "p3", "create_at": 300, "message": "sys", "type": "system_join_channel"},  # excluded
            },
        }

    async def test_reassembles_chronological_and_filters(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=self._sample_response())

        result = await rest.get_room_history("chan1", count=50)

        self.assertEqual([p["id"] for p in result], ["p1"])

    async def test_after_ts_is_epoch_ms_string_not_iso(self):
        rest = _make_rest(token="tok")
        rest._request = AsyncMock(return_value=self._sample_response())

        await rest.get_room_history("chan1", count=50, after_ts="150")

        params = rest._request.call_args.kwargs["params"]
        self.assertEqual(params["since"], 150)

    async def test_before_ts_client_side_filter(self):
        rest = _make_rest(token="tok")
        resp = {
            "order": ["p1", "p2"],
            "posts": {
                "p1": {"id": "p1", "create_at": 100, "message": "old", "type": ""},
                "p2": {"id": "p2", "create_at": 500, "message": "new", "type": ""},
            },
        }
        rest._request = AsyncMock(return_value=resp)

        result = await rest.get_room_history("chan1", count=50, before_ts="200")

        self.assertEqual([p["id"] for p in result], ["p1"])


class TestIsoToEpochMs(unittest.TestCase):
    def test_converts_iso_to_epoch_ms_string(self):
        result = iso_to_epoch_ms_str("2026-01-01T00:00:00+00:00")
        self.assertTrue(result.isdigit())
        self.assertEqual(int(result), 1767225600000)


# ── resolve_room ──────────────────────────────────────────────────────────────


class TestResolveRoom(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_dm(self):
        rest = _make_rest(token="tok")
        rest.bot_user_id = "bot-1"
        rest.get_user_by_username = AsyncMock(return_value={"id": "user-2"})
        rest._request = AsyncMock(return_value={"id": "dm-chan-1"})

        result = await rest.resolve_room("@alice")

        self.assertEqual(result, {"id": "dm-chan-1", "name": "@alice", "type": "dm"})
        rest._request.assert_called_once_with(
            "POST", "channels/direct", json_data=["bot-1", "user-2"]
        )

    async def test_dm_user_not_found_raises(self):
        rest = _make_rest(token="tok")
        rest.get_user_by_username = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )
        )
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("@nobody")

    async def test_resolves_channel_within_team(self):
        rest = _make_rest(token="tok")
        rest.team_id = "team-1"
        rest._request = AsyncMock(return_value={"id": "chan-1", "name": "general", "type": "O"})

        result = await rest.resolve_room("general")

        self.assertEqual(result, {"id": "chan-1", "name": "general", "type": "channel"})
        rest._request.assert_called_once_with("GET", "teams/team-1/channels/name/general")

    async def test_private_channel_type_mapped_to_group(self):
        rest = _make_rest(token="tok")
        rest.team_id = "team-1"
        rest._request = AsyncMock(return_value={"id": "chan-1", "name": "priv", "type": "P"})

        result = await rest.resolve_room("priv")
        self.assertEqual(result["type"], "group")

    async def test_channel_not_found_raises(self):
        rest = _make_rest(token="tok")
        rest.team_id = "team-1"
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )
        )
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("nonexistent")

    async def test_no_team_id_raises(self):
        rest = _make_rest(token="tok")
        with self.assertRaises(RuntimeError):
            await rest.resolve_room("general")


# ── 401 re-login behavior (login mode vs token mode) ─────────────────────────


class TestReloginBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_login_mode_retries_after_401(self):
        rest = _make_rest(username="bot", password="pw")
        rest._token = "expired"

        unauth = _make_response(401, {"message": "unauthorized"})
        ok = _make_response(200, {"success": True})
        rest._client.request = AsyncMock(side_effect=[unauth, ok])

        async def fake_login(u, p):
            rest._token = "fresh"
        rest.login = AsyncMock(side_effect=fake_login)

        result = await rest._request("GET", "some/endpoint")

        rest.login.assert_called_once()
        self.assertEqual(result, {"success": True})

    async def test_token_mode_does_not_retry_after_401(self):
        rest = _make_rest(token="abc")
        unauth = _make_response(401, {"message": "unauthorized"})
        rest._client.request = AsyncMock(return_value=unauth)

        with self.assertRaises(httpx.HTTPStatusError):
            await rest._request("GET", "some/endpoint")

        self.assertEqual(rest._client.request.call_count, 1)  # no retry attempted


if __name__ == "__main__":
    unittest.main()
