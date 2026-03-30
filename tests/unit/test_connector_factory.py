"""Unit tests for gateway.connectors.connector_factory().

Covers the three branches of the factory:
  - rocketchat  → RocketChatConnector
  - script      → ScriptConnector
  - unknown     → ValueError
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


def _make_cc(type_: str, name: str = "test") -> MagicMock:
    """Return a minimal ConnectorConfig-like mock."""
    cc = MagicMock()
    cc.type = type_
    cc.name = name
    return cc


class TestConnectorFactory(unittest.TestCase):
    def test_rocketchat_returns_rocketchat_connector(self):
        """connector_factory with type='rocketchat' must return a RocketChatConnector."""
        cc = _make_cc("rocketchat")

        mock_rc_config = MagicMock()
        mock_rc_connector_instance = MagicMock()
        mock_rc_connector_cls = MagicMock(return_value=mock_rc_connector_instance)
        mock_rc_config_cls = MagicMock()
        mock_rc_config_cls.from_connector_config.return_value = mock_rc_config

        with (
            patch(
                "gateway.connectors.rocketchat.RocketChatConnector",
                mock_rc_connector_cls,
            ),
            patch(
                "gateway.connectors.rocketchat.config.RocketChatConfig",
                mock_rc_config_cls,
            ),
        ):
            from gateway.connectors import connector_factory

            result = connector_factory(cc)

        mock_rc_config_cls.from_connector_config.assert_called_once_with(cc)
        mock_rc_connector_cls.assert_called_once_with(mock_rc_config)
        self.assertIs(result, mock_rc_connector_instance)

    def test_script_returns_script_connector(self):
        """connector_factory with type='script' must return a ScriptConnector."""
        cc = _make_cc("script", name="my-script")

        mock_script_instance = MagicMock()
        mock_script_cls = MagicMock(return_value=mock_script_instance)

        with patch("gateway.connectors.script.ScriptConnector", mock_script_cls):
            from gateway.connectors import connector_factory

            result = connector_factory(cc)

        mock_script_cls.assert_called_once_with(name="my-script")
        self.assertIs(result, mock_script_instance)

    def test_unknown_type_raises_value_error(self):
        """connector_factory with an unknown type must raise ValueError."""
        cc = _make_cc("telegram")

        from gateway.connectors import connector_factory

        with self.assertRaises(ValueError) as ctx:
            connector_factory(cc)

        self.assertIn("telegram", str(ctx.exception))
        self.assertIn("rocketchat", str(ctx.exception))
        self.assertIn("script", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
