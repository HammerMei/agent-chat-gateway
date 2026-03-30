"""Tests for GatewayConfig loading and validation.

Covers:
  - working_directory required and validated at config load (code_review Issue #6)
  - Config validation hardening: uniqueness, required fields, types (code_review)
  - cache_dir_global path resolution (code_review Issue #7)

Run with:
    uv run python -m pytest tests/test_config_loading.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import GatewayConfig

# ── Tests: working_directory validation ──────────────────────────────────────


class TestWorkingDirectoryValidation(unittest.TestCase):
    """Issue #6: working_directory must be required and validated at config load."""

    def _write_config(self, agents_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
{textwrap.indent(agents_block, "              ")}
            watchers:
              - name: w1
                room: general
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_missing_working_directory_raises(self):
        path = self._write_config("default:\n  type: claude")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("working_directory is required", str(ctx.exception))

    def test_empty_working_directory_raises(self):
        path = self._write_config('default:\n  type: claude\n  working_directory: ""')
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("working_directory is required", str(ctx.exception))

    def test_nonexistent_directory_raises(self):
        path = self._write_config(
            "default:\n  type: claude\n  working_directory: /nonexistent/path/xyz"
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("does not exist", str(ctx.exception))

    def test_valid_directory_accepted(self):
        path = self._write_config("default:\n  type: claude\n  working_directory: /tmp")
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")

    def test_relative_directory_resolved_to_config_dir(self):
        """A relative working_directory is resolved relative to the config file's directory."""
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "workdir"
            subdir.mkdir()
            path = self._write_config(
                "default:\n  type: claude\n  working_directory: workdir"
            )
            config_path = Path(tmpdir) / "config.yaml"
            shutil.move(path, config_path)

            config = GatewayConfig.from_file(str(config_path))
            self.assertEqual(
                config.agents["default"].working_directory,
                str(subdir.resolve()),
            )


# ── Tests: config validation hardening ───────────────────────────────────────


class TestConfigValidationHardening(unittest.TestCase):
    """Additional config validation for uniqueness, required fields, and types."""

    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_duplicate_connector_name_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
              - name: rc
                type: script
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Duplicate connector name 'rc'", str(ctx.exception))

    def test_duplicate_watcher_name_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: same
                room: general
              - name: same
                room: lobby
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Duplicate watcher name 'same'", str(ctx.exception))

    def test_empty_watcher_room_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: ""
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("must have a non-empty 'room' field", str(ctx.exception))

    def test_negative_max_queue_depth_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            max_queue_depth: -1
            watchers:
              - name: w1
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("max_queue_depth", str(ctx.exception))

    def test_non_mapping_agent_config_gets_clear_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default: claude
            watchers:
              - name: w1
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Agent 'default' config must be a mapping", str(ctx.exception))

    def test_non_mapping_watcher_entry_gets_clear_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - just-a-string
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Watcher entry at index 0 must be a mapping", str(ctx.exception))


# ── Tests: cache_dir_global path resolution ───────────────────────────────────


class TestCacheDirGlobalResolution(unittest.TestCase):
    """Issue #7: relative cache_dir_global must resolve relative to config directory."""

    def _write_config(self, cache_dir_global: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
                attachments:
                  cache_dir_global: {cache_dir_global}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_relative_cache_dir_resolved_to_config_dir(self):
        path = self._write_config("my-cache")
        config = GatewayConfig.from_file(path)
        config_dir = str(Path(path).parent.resolve())
        expected = str(Path(config_dir) / "my-cache")
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, expected)

    def test_absolute_cache_dir_unchanged(self):
        path = self._write_config("/absolute/cache/path")
        config = GatewayConfig.from_file(path)
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, "/absolute/cache/path")

    def test_tilde_cache_dir_unchanged(self):
        """Paths starting with ~ are left for expanduser() at connector init time."""
        path = self._write_config("~/.agent-chat-gateway/attachments")
        config = GatewayConfig.from_file(path)
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, "~/.agent-chat-gateway/attachments")


# ── Tests: env var expansion raises on unresolved placeholders (S3) ──────────


class TestEnvVarExpansionFailsOnUnresolved(unittest.TestCase):
    """S3: Unresolved env var placeholders must raise ValueError at config load,
    not silently use the literal placeholder string as a config value."""

    def _write_config_with_url(self, url: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: "{url}"
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_unresolved_dollar_brace_raises(self):
        """${UNSET_VAR} placeholder must raise ValueError, not be used literally."""
        import os
        # Ensure the variable is NOT set
        os.environ.pop("ACG_TEST_UNSET_12345", None)
        path = self._write_config_with_url("http://${ACG_TEST_UNSET_12345}:3000")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Unresolved environment variable", str(ctx.exception))
        self.assertIn("ACG_TEST_UNSET_12345", str(ctx.exception))

    def test_unresolved_dollar_plain_raises(self):
        """$UNSET_VAR placeholder (without braces) must also raise ValueError."""
        import os
        os.environ.pop("ACG_TEST_UNSET_99999", None)
        path = self._write_config_with_url("http://$ACG_TEST_UNSET_99999:3000")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Unresolved environment variable", str(ctx.exception))

    def test_resolved_env_var_does_not_raise(self):
        """A fully resolved env var must be accepted without error."""
        import os
        os.environ["ACG_TEST_SET_URL"] = "localhost"
        try:
            path = self._write_config_with_url("http://${ACG_TEST_SET_URL}:3000")
            # Should not raise — the var is set
            cfg = GatewayConfig.from_file(path)
            self.assertEqual(cfg.connectors[0].raw["server"]["url"], "http://localhost:3000")
        finally:
            os.environ.pop("ACG_TEST_SET_URL", None)


# ── Tests: ToolRule regex validated at config load time (S2) ─────────────────


class TestToolRuleRegexValidation(unittest.TestCase):
    """S2: Invalid regex patterns in ToolRule must raise ValueError at config
    load time, not silently fail or produce cryptic errors during runtime."""

    def _write_config_with_tool_rule(self, rule_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
{textwrap.indent(rule_block, "                  ")}
            watchers:
              - name: w1
                room: general
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_invalid_tool_regex_raises_at_load(self):
        """A bad regex in the 'tool' field must raise ValueError at config load."""
        path = self._write_config_with_tool_rule("- tool: '[invalid'")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("invalid tool rule", str(ctx.exception).lower())

    def test_invalid_params_regex_raises_at_load(self):
        """A bad regex in the 'params' field must raise ValueError at config load."""
        path = self._write_config_with_tool_rule(
            "- tool: Bash\n  params: '[unclosed'"
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_valid_tool_regex_accepted(self):
        """A valid regex in 'tool' must not raise."""
        path = self._write_config_with_tool_rule(
            "- tool: 'mcp__rocketchat__get_.*'\n  params: '.*'"
        )
        cfg = GatewayConfig.from_file(path)
        self.assertEqual(len(cfg.agents["default"].owner_allowed_tools), 1)
        self.assertEqual(cfg.agents["default"].owner_allowed_tools[0].tool,
                         "mcp__rocketchat__get_.*")


# ── Tests: GatewayConfig.agent raises KeyError on misconfiguration (Q2) ──────


class TestGatewayConfigAgentProperty(unittest.TestCase):
    """Q2: GatewayConfig.agent must raise KeyError instead of silently
    falling back to the first agent when default_agent is mismatched."""

    def test_agent_returns_correct_default(self):
        """When default_agent is valid, .agent returns the right config."""
        from gateway.core.config import AgentConfig
        cfg = GatewayConfig(
            connectors=[],
            agents={"main": AgentConfig(name="main"), "other": AgentConfig(name="other")},
            default_agent="main",
        )
        self.assertEqual(cfg.agent.name, "main")

    def test_agent_raises_on_missing_default(self):
        """When default_agent is not in agents, .agent must raise KeyError."""
        from gateway.core.config import AgentConfig
        cfg = GatewayConfig(
            connectors=[],
            agents={"main": AgentConfig(name="main")},
            default_agent="nonexistent",
        )
        with self.assertRaises(KeyError) as ctx:
            _ = cfg.agent
        self.assertIn("nonexistent", str(ctx.exception))

    def test_agent_error_message_lists_available_agents(self):
        """KeyError message must include available agent names for diagnosis."""
        from gateway.core.config import AgentConfig
        cfg = GatewayConfig(
            connectors=[],
            agents={"alpha": AgentConfig(name="alpha"), "beta": AgentConfig(name="beta")},
            default_agent="gamma",
        )
        with self.assertRaises(KeyError) as ctx:
            _ = cfg.agent
        error_str = str(ctx.exception)
        self.assertIn("gamma", error_str)


if __name__ == "__main__":
    unittest.main()
