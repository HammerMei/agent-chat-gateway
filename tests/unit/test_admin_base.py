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
    emails_match,
)


class TestEmailsMatch(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(emails_match("a@x.com", "a@x.com"))

    def test_case_insensitive_match(self):
        self.assertTrue(emails_match("Alice@X.COM", "alice@x.com"))

    def test_whitespace_insensitive_match(self):
        self.assertTrue(emails_match("  a@x.com  ", "a@x.com"))

    def test_genuine_mismatch(self):
        self.assertFalse(emails_match("a@x.com", "b@x.com"))

    def test_empty_existing_email_fails_open_to_match(self):
        # Deliberate, documented trade-off (see emails_match's docstring),
        # not a claim that it's provably the same identity.
        self.assertTrue(emails_match("", "anything@x.com"))

    def test_empty_requested_email_against_real_existing_is_a_mismatch(self):
        # Only an EMPTY existing_email fails open — an empty requested
        # email against a real existing one is a genuine mismatch.
        self.assertFalse(emails_match("a@x.com", ""))


class TestExceptionPayloads(unittest.TestCase):
    def test_user_already_exists_carries_existing_user(self):
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        err = UserAlreadyExistsError("alice", existing=existing)
        self.assertEqual(err.username, "alice")
        self.assertIs(err.existing, existing)
        self.assertIn("alice", str(err))

    def test_user_already_exists_identity_matches_defaults_to_true(self):
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        err = UserAlreadyExistsError("alice", existing=existing)
        self.assertTrue(err.identity_matches)

    def test_user_already_exists_identity_matches_explicit_false(self):
        existing = AdminUser(id="u1", username="alice", email="someone-else@x.com")
        err = UserAlreadyExistsError("alice", existing=existing, identity_matches=False)
        self.assertFalse(err.identity_matches)

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

    async def create_user(self, username, email, password, *, full_name=None):
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
