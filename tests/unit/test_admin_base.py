"""Unit tests for gateway.admin.base: exception payloads and the
PlatformAdmin async context manager contract."""

from __future__ import annotations

import unittest

from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    PlatformAdmin,
    UserAlreadyExistsError,
)


class TestExceptionPayloads(unittest.TestCase):
    def test_user_already_exists_carries_existing_user(self):
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        err = UserAlreadyExistsError("alice", existing=existing)
        self.assertEqual(err.username, "alice")
        self.assertIs(err.existing, existing)
        self.assertIn("alice", str(err))

    def test_channel_already_exists_carries_existing_channel(self):
        existing = AdminChannel(id="c1", name="general", is_private=False)
        err = ChannelAlreadyExistsError("general", existing=existing)
        self.assertEqual(err.name, "general")
        self.assertIs(err.existing, existing)
        self.assertIn("general", str(err))


class _DummyAdmin(PlatformAdmin):
    """Minimal concrete PlatformAdmin to exercise __aenter__/__aexit__."""

    def __init__(self, fail_connect: bool = False):
        self.connected = False
        self.closed = False
        self._fail_connect = fail_connect

    async def connect(self):
        if self._fail_connect:
            raise RuntimeError("bad credentials")
        self.connected = True

    async def close(self):
        self.closed = True

    async def create_user(self, username, email, password, *, full_name=None, verified=False):
        raise NotImplementedError

    async def create_channel(self, name, *, is_private=False):
        raise NotImplementedError

    async def add_user_to_channel(self, username, channel_name):
        raise NotImplementedError

    async def delete_user(self, username):
        raise NotImplementedError

    async def delete_channel(self, channel_name):
        raise NotImplementedError


class TestPlatformAdminContextManager(unittest.IsolatedAsyncioTestCase):
    async def test_context_manager_connects_and_closes(self):
        admin = _DummyAdmin()
        async with admin as ctx:
            self.assertIs(ctx, admin)
            self.assertTrue(admin.connected)
            self.assertFalse(admin.closed)
        self.assertTrue(admin.closed)

    async def test_closes_and_reraises_when_connect_fails(self):
        # Python's async-with protocol never calls __aexit__ if __aenter__
        # raises — __aenter__ itself must clean up, or the httpx.AsyncClient
        # instances allocated in the constructor leak.
        admin = _DummyAdmin(fail_connect=True)

        with self.assertRaises(RuntimeError):
            async with admin:
                pass

        self.assertTrue(admin.closed)


if __name__ == "__main__":
    unittest.main()
