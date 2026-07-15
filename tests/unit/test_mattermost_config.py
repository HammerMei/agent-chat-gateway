"""Unit tests for MattermostConfig.

Covers:
  - Dual-auth validation (token XOR username+password)
  - from_connector_config() parsing of server/allowed_users/attachments/agent_chain
  - role_of / allow_senders
"""

from __future__ import annotations

import unittest

from gateway.config import ConnectorConfig
from gateway.connectors.mattermost.config import MattermostConfig
from gateway.core.agent_chain import AgentChainConfig

# ── Dual-auth validation ─────────────────────────────────────────────────────


class TestDualAuthValidation(unittest.TestCase):
    def test_token_only_is_valid(self):
        cfg = MattermostConfig(server_url="https://x", team="t", token="tok")
        self.assertEqual(cfg.token, "tok")

    def test_username_password_only_is_valid(self):
        cfg = MattermostConfig(server_url="https://x", team="t", username="u", password="p")
        self.assertEqual(cfg.username, "u")

    def test_both_token_and_password_raises(self):
        with self.assertRaises(ValueError):
            MattermostConfig(
                server_url="https://x", team="t", token="tok", username="u", password="p"
            )

    def test_neither_raises(self):
        with self.assertRaises(ValueError):
            MattermostConfig(server_url="https://x", team="t")

    def test_username_without_password_raises(self):
        with self.assertRaises(ValueError):
            MattermostConfig(server_url="https://x", team="t", username="u")

    def test_password_without_username_raises(self):
        with self.assertRaises(ValueError):
            MattermostConfig(server_url="https://x", team="t", password="p")


# ── role_of / allow_senders ──────────────────────────────────────────────────


class TestRoleOf(unittest.TestCase):
    def _config(self):
        return MattermostConfig(
            server_url="https://x", team="t", token="tok",
            owners=["alice"], guests=["bob"],
        )

    def test_owner_role(self):
        self.assertEqual(self._config().role_of("alice"), "owner")

    def test_guest_role(self):
        self.assertEqual(self._config().role_of("bob"), "guest")

    def test_unknown_defaults_to_guest(self):
        self.assertEqual(self._config().role_of("mallory"), "guest")

    def test_allow_senders_is_owners_plus_guests(self):
        cfg = self._config()
        self.assertEqual(set(cfg.allow_senders), {"alice", "bob"})


# ── from_connector_config ────────────────────────────────────────────────────


class TestFromConnectorConfig(unittest.TestCase):
    def test_parses_token_auth(self):
        cc = ConnectorConfig(
            name="mm1",
            type="mattermost",
            raw={
                "server": {"url": "https://chat.example.com/", "team": "myteam", "token": "abc123"},
                "allowed_users": {"owners": ["alice"], "guests": ["bob"]},
            },
        )
        cfg = MattermostConfig.from_connector_config(cc)
        self.assertEqual(cfg.server_url, "https://chat.example.com")  # trailing slash stripped
        self.assertEqual(cfg.team, "myteam")
        self.assertEqual(cfg.token, "abc123")
        self.assertEqual(cfg.username, "")
        self.assertEqual(cfg.owners, ["alice"])
        self.assertEqual(cfg.guests, ["bob"])
        self.assertEqual(cfg.name, "mm1")

    def test_parses_password_auth(self):
        cc = ConnectorConfig(
            name="mm1",
            type="mattermost",
            raw={
                "server": {"url": "https://x", "team": "t", "username": "bot", "password": "pw"},
            },
        )
        cfg = MattermostConfig.from_connector_config(cc)
        self.assertEqual(cfg.username, "bot")
        self.assertEqual(cfg.password, "pw")
        self.assertEqual(cfg.token, "")

    def test_defaults_when_optional_fields_absent(self):
        cc = ConnectorConfig(
            name="mm1",
            type="mattermost",
            raw={"server": {"url": "https://x", "team": "t", "token": "tok"}},
        )
        cfg = MattermostConfig.from_connector_config(cc)
        self.assertEqual(cfg.owners, [])
        self.assertEqual(cfg.guests, [])
        self.assertTrue(cfg.require_mention)
        self.assertTrue(cfg.filter_sender)
        self.assertFalse(cfg.reply_in_thread)
        self.assertTrue(cfg.permission_reply_in_thread)
        self.assertEqual(cfg.timezone, "")
        self.assertEqual(cfg.attachments.max_file_size_mb, 10.0)

    def test_parses_agent_chain(self):
        cc = ConnectorConfig(
            name="mm1",
            type="mattermost",
            raw={
                "server": {"url": "https://x", "team": "t", "token": "tok"},
                "agent_chain": {"agent_usernames": ["peer"], "max_turns": 7, "ttl_seconds": 120.0},
            },
        )
        cfg = MattermostConfig.from_connector_config(cc)
        self.assertEqual(cfg.agent_chain, AgentChainConfig(agent_usernames=["peer"], max_turns=7, ttl_seconds=120.0))

    def test_parses_attachments_overrides(self):
        cc = ConnectorConfig(
            name="mm1",
            type="mattermost",
            raw={
                "server": {"url": "https://x", "team": "t", "token": "tok"},
                "attachments": {"max_file_size_mb": 5.0, "download_timeout": 15},
            },
        )
        cfg = MattermostConfig.from_connector_config(cc)
        self.assertEqual(cfg.attachments.max_file_size_mb, 5.0)
        self.assertEqual(cfg.attachments.download_timeout, 15)


if __name__ == "__main__":
    unittest.main()
