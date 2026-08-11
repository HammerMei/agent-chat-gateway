"""Unit tests for gateway.admin.factory.admin_factory."""

from __future__ import annotations

import unittest

from gateway.admin.config import AdminConfigError, AdminProfile
from gateway.admin.factory import admin_factory
from gateway.admin.mattermost_admin import MattermostAdmin
from gateway.admin.rocketchat_admin import RocketChatAdmin


class TestAdminFactory(unittest.TestCase):
    def test_rocketchat_type_returns_rocketchat_admin(self):
        profile = AdminProfile(
            name="rc", type="rocketchat", server_url="https://x",
            username="admin", password="pw",
        )
        admin = admin_factory(profile)
        self.assertIsInstance(admin, RocketChatAdmin)

    def test_mattermost_type_returns_mattermost_admin(self):
        profile = AdminProfile(
            name="mm", type="mattermost", server_url="https://x", team="t", token="tok"
        )
        admin = admin_factory(profile)
        self.assertIsInstance(admin, MattermostAdmin)

    def test_unsupported_type_raises(self):
        profile = AdminProfile(
            name="mm", type="mattermost", server_url="https://x", team="t", token="tok"
        )
        profile.type = "slack"  # bypass __post_init__ validation to hit factory's own guard
        with self.assertRaises(AdminConfigError):
            admin_factory(profile)


if __name__ == "__main__":
    unittest.main()
