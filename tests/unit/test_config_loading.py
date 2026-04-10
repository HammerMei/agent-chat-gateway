"""Tests for GatewayConfig loading and validation.

Covers:
  - working_directory required and validated at config load (code_review Issue #6)
  - Config validation hardening: uniqueness, required fields, types (code_review)
  - cache_dir_global path resolution (code_review Issue #7)
  - Built-in context auto-injection via context_inject_files_for() (layer-0 system files)

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


# ── Tests: built-in context auto-injection ────────────────────────────────────


class TestBuiltinContextAutoInjection(unittest.TestCase):
    """Built-in system context files are auto-prepended in context_inject_files_for().

    rc-gateway-context.md  → injected for every Rocket.Chat connector
    scheduling-context.md  → injected for every configured connector
    """

    def _make_core_config(self, connector_type: str, user_ctx: list[str] | None = None):
        from gateway.core.config import AgentConfig, ConnectorConfig, CoreConfig
        connector = ConnectorConfig(
            name="rc",
            type=connector_type,
            raw={},
            context_inject_files=user_ctx or [],
        )
        agent = AgentConfig(name="default", timeout=10)
        return CoreConfig(
            connector_configs={"rc": connector},
            agents={"default": agent},
            default_agent="default",
        )

    def test_rc_connector_gets_both_builtin_files(self):
        """RC connector → rc-gateway-context.md + scheduling-context.md prepended."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)

    def test_rc_gateway_context_comes_before_scheduling(self):
        """rc-gateway-context.md must appear before scheduling-context.md."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        rc_idx = basenames.index("rc-gateway-context.md")
        sched_idx = basenames.index("scheduling-context.md")
        self.assertLess(rc_idx, sched_idx)

    def test_non_rc_connector_gets_scheduling_only(self):
        """Non-RC connector → only scheduling-context.md injected (no rc-gateway-context.md)."""
        config = self._make_core_config("script")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertNotIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)

    def test_builtin_files_precede_user_connector_files(self):
        """Built-in layer 0 files must come before user-configured connector files."""
        config = self._make_core_config("rocketchat", user_ctx=["/user/custom.md"])
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("scheduling-context.md")
        custom_idx = basenames.index("custom.md")
        self.assertLess(sched_idx, custom_idx)

    def test_builtin_files_are_absolute_paths(self):
        """Built-in file paths must be absolute (so ContextInjector can read them)."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "scheduling-context.md")]
        for f in builtin:
            self.assertTrue(Path(f).is_absolute(), f"{f} must be absolute")

    def test_builtin_files_actually_exist_in_package(self):
        """Built-in context files must be present in the installed package."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "scheduling-context.md")]
        for f in builtin:
            self.assertTrue(Path(f).exists(), f"Built-in context file missing: {f}")

    def test_no_connector_config_skips_builtin_injection(self):
        """When ConnectorConfig is absent (e.g. tests), no built-in files are prepended."""
        from gateway.core.config import AgentConfig, CoreConfig
        config = CoreConfig(agents={"default": AgentConfig(timeout=10)}, default_agent="default")
        files = config.context_inject_files_for("unknown-connector", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertNotIn("rc-gateway-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)

    def test_watcher_user_files_appended_after_builtin(self):
        """Watcher-level user files are appended last, after all built-in and agent files."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", ["/watcher/extra.md"])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("scheduling-context.md")
        extra_idx = basenames.index("extra.md")
        self.assertLess(sched_idx, extra_idx)


# ── Tests: built-in owner tool rule auto-injection ────────────────────────────


class TestBuiltinOwnerToolRuleAutoInjection(unittest.TestCase):
    """Built-in gateway tool rules are always prepended to owner_allowed_tools.

    These ensure that `agent-chat-gateway send`, `agent-chat-gateway schedule`,
    and `date` never require a 🔐 human-approval prompt.  The first two are the
    gateway's own commands; `date` is a read-only command used by agents to
    compute timestamps in compound bash expressions (e.g. ``$(date ...)``).
    """

    def _make_agent(self, extra_rules=None):
        from gateway.core.config import AgentConfig, ToolRule
        rules = []
        if extra_rules:
            rules = [ToolRule(tool="Bash", params=p) for p in extra_rules]
        return AgentConfig(name="test", owner_allowed_tools=rules)

    def test_effective_includes_send_rule(self):
        """effective_owner_allowed_tools() must include 'agent-chat-gateway send .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("agent-chat-gateway" in (p or "") and "send" in (p or "") for p in params),
            "Built-in send rule must be in effective_owner_allowed_tools",
        )

    def test_effective_includes_schedule_rule(self):
        """effective_owner_allowed_tools() must include 'agent-chat-gateway schedule .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("agent-chat-gateway" in (p or "") and "schedule" in (p or "") for p in params),
            "Built-in schedule rule must be in effective_owner_allowed_tools",
        )

    def test_builtin_rules_precede_user_rules(self):
        """Built-in rules must come before user-defined rules."""
        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        agent = self._make_agent(extra_rules=["git log.*"])
        effective = agent.effective_owner_allowed_tools()
        builtin_count = len(_BUILTIN_OWNER_TOOL_RULES)
        # First N entries must match the built-in rules
        for i, builtin_rule in enumerate(_BUILTIN_OWNER_TOOL_RULES):
            self.assertEqual(effective[i].tool, builtin_rule.tool)
            self.assertEqual(effective[i].params, builtin_rule.params)
        # The user rule follows after
        self.assertEqual(effective[builtin_count].params, "git log.*")

    def test_owner_allowed_tools_field_unchanged(self):
        """effective_owner_allowed_tools() must NOT mutate owner_allowed_tools in place."""
        from gateway.core.config import ToolRule
        user_rule = ToolRule(tool="Bash", params="ls.*")
        agent = self._make_agent()
        agent.owner_allowed_tools = [user_rule]
        original_len = len(agent.owner_allowed_tools)
        agent.effective_owner_allowed_tools()
        self.assertEqual(len(agent.owner_allowed_tools), original_len)

    def test_empty_owner_list_still_gets_builtins(self):
        """An agent with an empty owner_allowed_tools still gets the built-in rules."""
        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        self.assertGreaterEqual(len(effective), len(_BUILTIN_OWNER_TOOL_RULES))

    def test_send_rule_matches_actual_command(self):
        """The send rule must actually match a realistic agent-chat-gateway send command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        send_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "send" in r.params)
        cmd = 'agent-chat-gateway send general "Hello RC"'
        self.assertIsNotNone(
            re.fullmatch(send_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Send rule {send_rule.params!r} must match command {cmd!r}",
        )

    def test_schedule_rule_matches_actual_command(self):
        """The schedule rule must actually match a realistic agent-chat-gateway schedule command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        sched_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "schedule" in r.params)
        cmd = 'agent-chat-gateway schedule create dm "Remind me to cook" --every 1d --at 09:00'
        self.assertIsNotNone(
            re.fullmatch(sched_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Schedule rule {sched_rule.params!r} must match command {cmd!r}",
        )

    def test_effective_includes_date_rule(self):
        """effective_owner_allowed_tools() must include a rule that auto-approves date."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("date" in (p or "") for p in params),
            "Built-in date rule must be in effective_owner_allowed_tools",
        )

    def test_date_rule_matches_bare_date(self):
        """The date rule must match 'date' with no arguments."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        date_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "date" in r.params
                         and "agent-chat-gateway" not in r.params)
        cmd = "date"
        self.assertIsNotNone(
            re.fullmatch(date_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Date rule {date_rule.params!r} must match command {cmd!r}",
        )

    def test_date_rule_matches_date_with_flags(self):
        """The date rule must match date commands used for timestamp calculation."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        date_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "date" in r.params
                         and "agent-chat-gateway" not in r.params)
        cmd = "date -v+1M '+%Y-%m-%d %H:%M'"
        self.assertIsNotNone(
            re.fullmatch(date_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Date rule {date_rule.params!r} must match command {cmd!r}",
        )


if __name__ == "__main__":
    unittest.main()
