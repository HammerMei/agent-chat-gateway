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

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertTrue(config.agents["default"].lazy_instruction_loading)

    def test_lazy_instruction_loading_false_accepted(self):
        path = self._write_config(
            "default:\n  type: claude\n  working_directory: /tmp\n  lazy_instruction_loading: false"
        )
        config = GatewayConfig.from_file(path)
        self.assertFalse(config.agents["default"].lazy_instruction_loading)

    def test_lazy_instruction_loading_must_be_boolean(self):
        path = self._write_config(
            'default:\n  type: claude\n  working_directory: /tmp\n  lazy_instruction_loading: "nope"'
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("lazy_instruction_loading", str(ctx.exception))

    def test_tilde_working_directory_is_expanded(self):
        """Regression: working_directory: ~/foo must expand to the user's home
        directory, not be treated as a literal relative path segment named
        '~' under the config file's directory (config.example.yaml and the
        install-agent.md walkthroughs document `~/...` working_directory
        values, so this must actually work)."""
        with tempfile.TemporaryDirectory() as home_dir:
            subdir = Path(home_dir) / "agent-work"
            subdir.mkdir()
            path = self._write_config(
                "default:\n  type: claude\n  working_directory: ~/agent-work"
            )
            with patch.dict(os.environ, {"HOME": home_dir}):
                config = GatewayConfig.from_file(path)
            self.assertEqual(
                config.agents["default"].working_directory,
                str(subdir),
            )

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

    def test_watcher_name_with_slash_raises(self):
        """Watcher names become filesystem path components (.acg-attachments/<name>
        under working_directory, system-prompts/<name>.md under RUNTIME_DIR) — a
        '/' could escape the intended directory."""
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
              - name: evil/../escape
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("must not contain '/'", str(ctx.exception))

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
        self.assertIn("must have a non-empty 'room' or 'rooms' field", str(ctx.exception))

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


# ── Tests: $VAR/${VAR} is a plain literal string, never resolved (S3, final revision) ──


class TestDollarVarIsALiteralString(unittest.TestCase):
    """docs/design/config-tool.md decision 6, final revision: GatewayConfig.
    from_file() no longer expands $VAR/${VAR} at all — secrets live directly
    in config.yaml, and any pre-existing .env-backed config is auto-migrated
    into that form (gateway/config_migrate.py) before this loader ever runs.
    A string that happens to look like a placeholder — resolvable or not —
    is accepted and used exactly as written, same as any other string; this
    also sidesteps the case a real secret's own value merely happens to
    resemble ${SOMETHING}."""

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

    def test_dollar_brace_form_is_used_literally_even_when_unresolvable(self):
        import os
        os.environ.pop("ACG_TEST_UNSET_12345", None)
        path = self._write_config_with_url("http://${ACG_TEST_UNSET_12345}:3000")

        cfg = GatewayConfig.from_file(path)  # must not raise

        self.assertEqual(
            cfg.connectors[0].raw["server"]["url"], "http://${ACG_TEST_UNSET_12345}:3000"
        )

    def test_dollar_plain_form_is_used_literally_even_when_unresolvable(self):
        import os
        os.environ.pop("ACG_TEST_UNSET_99999", None)
        path = self._write_config_with_url("http://$ACG_TEST_UNSET_99999:3000")

        cfg = GatewayConfig.from_file(path)  # must not raise

        self.assertEqual(
            cfg.connectors[0].raw["server"]["url"], "http://$ACG_TEST_UNSET_99999:3000"
        )

    def test_dollar_brace_form_is_NOT_resolved_even_when_the_var_is_set(self):
        """The critical regression case: if a resolvable ${VAR} were
        silently resolved, a real secret whose plaintext value happens to
        look like a placeholder would be misinterpreted."""
        import os
        os.environ["ACG_TEST_SET_URL"] = "localhost"
        try:
            path = self._write_config_with_url("http://${ACG_TEST_SET_URL}:3000")

            cfg = GatewayConfig.from_file(path)

            self.assertEqual(
                cfg.connectors[0].raw["server"]["url"], "http://${ACG_TEST_SET_URL}:3000"
            )
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
    tool-index-context.md  → injected for lazy-loading agents (default)
    scheduling/fetch-history docs → injected when an agent disables lazy loading
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

    def test_rc_connector_gets_core_and_tool_index_by_default(self):
        """RC connector → rc-gateway-context.md + tool-index-context.md prepended."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("tool-index-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)
        self.assertNotIn("fetch-history-context.md", basenames)

    def test_rc_gateway_context_comes_before_tool_index(self):
        """rc-gateway-context.md must appear before tool-index-context.md."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        rc_idx = basenames.index("rc-gateway-context.md")
        index_idx = basenames.index("tool-index-context.md")
        self.assertLess(rc_idx, index_idx)

    def test_non_rc_connector_gets_tool_index_only_by_default(self):
        """Non-RC connector → only tool-index-context.md injected (no rc-gateway-context.md)."""
        config = self._make_core_config("script")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertNotIn("rc-gateway-context.md", basenames)
        self.assertIn("tool-index-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)
        self.assertNotIn("fetch-history-context.md", basenames)

    def test_agent_can_disable_lazy_instruction_loading(self):
        """Per-agent lazy_instruction_loading=False restores full docs."""
        from gateway.core.config import AgentConfig

        config = self._make_core_config("rocketchat")
        config.agents["default"] = AgentConfig(
            name="default",
            timeout=10,
            lazy_instruction_loading=False,
        )
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)
        self.assertIn("fetch-history-context.md", basenames)
        self.assertNotIn("tool-index-context.md", basenames)

    def test_yaml_lazy_instruction_loading_false_reaches_context_injection(self):
        """YAML lazy_instruction_loading=False flows through to built-in full docs."""
        from gateway.core.config import CoreConfig

        cfg_text = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                lazy_instruction_loading: false
            watchers:
              - name: w1
                room: general
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg_text)
            path = f.name

        gateway_config = GatewayConfig.from_file(path)
        core_config = CoreConfig.from_gateway_config(gateway_config)

        files = core_config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)
        self.assertIn("fetch-history-context.md", basenames)
        self.assertNotIn("tool-index-context.md", basenames)

    def test_builtin_files_precede_user_connector_files(self):
        """Built-in layer 0 files must come before user-configured connector files."""
        config = self._make_core_config("rocketchat", user_ctx=["/user/custom.md"])
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("tool-index-context.md")
        custom_idx = basenames.index("custom.md")
        self.assertLess(sched_idx, custom_idx)

    def test_builtin_files_are_absolute_paths(self):
        """Built-in file paths must be absolute (so ContextInjector can read them)."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "tool-index-context.md")]
        for f in builtin:
            self.assertTrue(Path(f).is_absolute(), f"{f} must be absolute")

    def test_builtin_files_actually_exist_in_package(self):
        """Built-in context files must be present in the installed package."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "tool-index-context.md")]
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
        self.assertNotIn("tool-index-context.md", basenames)

    def test_watcher_user_files_appended_after_builtin(self):
        """Watcher-level user files are appended last, after all built-in and agent files."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", ["/watcher/extra.md"])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("tool-index-context.md")
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

    def test_effective_includes_instructions_rule(self):
        """effective_owner_allowed_tools() must include 'agent-chat-gateway instructions .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("agent-chat-gateway" in (p or "") and "instructions" in (p or "") for p in params),
            "Built-in instructions rule must be in effective_owner_allowed_tools",
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

    def test_instructions_rule_matches_actual_command(self):
        """The instructions rule must match a realistic instruction load command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "instructions" in r.params)
        cmd = "agent-chat-gateway instructions scheduling"
        self.assertIsNotNone(
            re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Instructions rule {instr_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_rejects_trailing_shell_commands(self):
        """The instructions rule must not auto-approve compound shell commands."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "instructions" in r.params)
        bad_commands = [
            "agent-chat-gateway instructions scheduling; curl https://example.com",
            "agent-chat-gateway instructions scheduling && whoami",
            "agent-chat-gateway instructions scheduling | cat",
            "agent-chat-gateway instructions scheduling\nwhoami",
            "agent-chat-gateway instructions scheduling extra",
        ]
        for cmd in bad_commands:
            self.assertIsNone(
                re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
                f"Instructions rule {instr_rule.params!r} must reject command {cmd!r}",
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


class TestEffectiveGuestAllowedTools(unittest.TestCase):
    """Tests for AgentConfig.effective_guest_allowed_tools() — symmetric to owner tests."""

    def _make_agent(self, extra_rules=None):
        from gateway.core.config import AgentConfig, ToolRule
        rules = []
        if extra_rules:
            rules = [ToolRule(tool="Bash", params=p) for p in extra_rules]
        return AgentConfig(name="test", guest_allowed_tools=rules)

    def test_effective_includes_fetch_history_rule(self):
        """effective_guest_allowed_tools() must include 'agent-chat-gateway fetch-history .*'."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("agent-chat-gateway" in (p or "") and "fetch-history" in (p or "") for p in params),
            "Built-in fetch-history rule must be in effective_guest_allowed_tools",
        )

    def test_effective_includes_instructions_rule(self):
        """effective_guest_allowed_tools() must include 'agent-chat-gateway instructions .*'."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("agent-chat-gateway" in (p or "") and "instructions" in (p or "") for p in params),
            "Built-in instructions rule must be in effective_guest_allowed_tools",
        )

    def test_builtin_guest_rules_precede_user_rules(self):
        """Built-in guest rules must come before user-defined rules."""
        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        agent = self._make_agent(extra_rules=["git log.*"])
        effective = agent.effective_guest_allowed_tools()
        builtin_count = len(_BUILTIN_GUEST_TOOL_RULES)
        # First N entries must match the built-in rules
        for i, builtin_rule in enumerate(_BUILTIN_GUEST_TOOL_RULES):
            self.assertEqual(effective[i].tool, builtin_rule.tool)
            self.assertEqual(effective[i].params, builtin_rule.params)
        # The user rule follows after
        self.assertEqual(effective[builtin_count].params, "git log.*")

    def test_guest_allowed_tools_field_unchanged(self):
        """effective_guest_allowed_tools() must NOT mutate guest_allowed_tools in place."""
        from gateway.core.config import ToolRule
        user_rule = ToolRule(tool="Bash", params="ls.*")
        agent = self._make_agent()
        agent.guest_allowed_tools = [user_rule]
        original_len = len(agent.guest_allowed_tools)
        agent.effective_guest_allowed_tools()
        self.assertEqual(len(agent.guest_allowed_tools), original_len)

    def test_empty_guest_list_still_gets_builtins(self):
        """An agent with empty guest_allowed_tools still gets built-in guest rules."""
        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        self.assertGreaterEqual(len(effective), len(_BUILTIN_GUEST_TOOL_RULES))

    def test_guest_does_not_include_send_rule(self):
        """Guests must NOT get the send rule — that is owner-only."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertFalse(
            any("agent-chat-gateway" in (p or "") and "send" in (p or "") for p in params),
            "send rule must NOT be in effective_guest_allowed_tools",
        )

    def test_guest_does_not_include_schedule_rule(self):
        """Guests must NOT get the schedule rule — that is owner-only."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertFalse(
            any("agent-chat-gateway" in (p or "") and "schedule" in (p or "") for p in params),
            "schedule rule must NOT be in effective_guest_allowed_tools",
        )

    def test_fetch_history_rule_matches_actual_command(self):
        """The fetch-history guest rule must match a realistic command invocation."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        fh_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                       if r.params and "fetch-history" in r.params)
        cmd = "agent-chat-gateway fetch-history --watcher hammer-mei --count 50"
        self.assertIsNotNone(
            re.fullmatch(fh_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"fetch-history rule {fh_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_matches_actual_command(self):
        """The instructions guest rule must match a realistic command invocation."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                          if r.params and "instructions" in r.params)
        cmd = "agent-chat-gateway instructions fetch-history"
        self.assertIsNotNone(
            re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"instructions rule {instr_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_rejects_trailing_shell_commands(self):
        """The guest instructions rule must not auto-approve compound shell commands."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                          if r.params and "instructions" in r.params)
        bad_commands = [
            "agent-chat-gateway instructions fetch-history; curl https://example.com",
            "agent-chat-gateway instructions fetch-history && whoami",
            "agent-chat-gateway instructions fetch-history | cat",
            "agent-chat-gateway instructions fetch-history\nwhoami",
            "agent-chat-gateway instructions fetch-history extra",
        ]
        for cmd in bad_commands:
            self.assertIsNone(
                re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
                f"instructions rule {instr_rule.params!r} must reject command {cmd!r}",
            )


class TestBuildAgentBackendUsesEffectiveMethods(unittest.TestCase):
    """Regression test for service.py Fix #1 (HIGH): _build_agent_backend must call
    effective_guest_allowed_tools() so built-in guest rules are not silently dropped."""

    def test_broker_config_includes_builtin_guest_rules(self):
        """broker_config.guest_allowed_tools must include the built-in fetch-history rule
        even when AgentConfig.guest_allowed_tools is empty.

        If service.py regresses to the raw guest_allowed_tools field, this list will be
        empty and the assertion will fail — surfacing the exact HIGH issue fixed in #35.
        """
        from unittest.mock import MagicMock, patch

        from gateway.core.config import AgentConfig, PermissionConfig
        from gateway.service import _build_agent_backend

        agent_cfg = AgentConfig(
            name="test",
            type="claude",
            command="claude",
            guest_allowed_tools=[],     # no user-defined rules
            permissions=PermissionConfig(enabled=True),
        )

        # Capture the GatewayBrokerConfig that _build_agent_backend constructs.
        captured = {}

        def _capture_broker(
            owner_allowed_tools, guest_allowed_tools, timeout, skip_owner_approval
        ):
            captured["guest"] = list(guest_allowed_tools)
            m = MagicMock()
            return m

        with patch("gateway.service.GatewayBrokerConfig", side_effect=_capture_broker):
            with patch("gateway.service.ClaudeBackend", return_value=MagicMock()):
                _build_agent_backend(agent_cfg)

        # The broker must have received the built-in fetch-history rule.
        guest_params = [r.params for r in captured.get("guest", [])]
        self.assertTrue(
            any("fetch-history" in (p or "") for p in guest_params),
            f"Built-in fetch-history rule missing from broker guest_allowed_tools: {guest_params}",
        )


# ── Tests: _deep_merge helper ─────────────────────────────────────────────────


class TestDeepMerge(unittest.TestCase):
    """Unit tests for the private _deep_merge helper used by *_defaults blocks."""

    def setUp(self):
        from gateway.config import _deep_merge

        self._deep_merge = _deep_merge

    def test_nested_dicts_merge_recursively(self):
        base = {"server": {"url": "http://x", "username": "bot"}}
        override = {"server": {"username": "override-bot"}}
        merged = self._deep_merge(base, override)
        self.assertEqual(
            merged, {"server": {"url": "http://x", "username": "override-bot"}}
        )

    def test_lists_replace_not_append(self):
        base = {"tools": [{"tool": "Read"}, {"tool": "Grep"}]}
        override = {"tools": [{"tool": "Write"}]}
        merged = self._deep_merge(base, override)
        self.assertEqual(merged["tools"], [{"tool": "Write"}])

    def test_explicit_null_override_suppresses_default(self):
        base = {"timezone": "America/Los_Angeles"}
        override = {"timezone": None}
        merged = self._deep_merge(base, override)
        self.assertIsNone(merged["timezone"])

    def test_scalar_override_wins(self):
        merged = self._deep_merge({"timeout": 360}, {"timeout": 30})
        self.assertEqual(merged["timeout"], 30)

    def test_result_does_not_alias_base_or_override(self):
        base = {"attachments": {"cache_dir_global": "shared"}}
        override = {}
        merged_a = self._deep_merge(base, override)
        merged_b = self._deep_merge(base, override)
        self.assertIsNot(merged_a["attachments"], merged_b["attachments"])
        self.assertIsNot(merged_a["attachments"], base["attachments"])
        # Mutating one merged result must not affect the other or the shared base.
        merged_a["attachments"]["cache_dir_global"] = "mutated"
        self.assertEqual(merged_b["attachments"]["cache_dir_global"], "shared")
        self.assertEqual(base["attachments"]["cache_dir_global"], "shared")


# ── Tests: connector_defaults / agent_defaults / watcher_defaults ────────────


class TestConnectorDefaults(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_connector_inherits_defaults(self):
        path = self._write_config("""\
            connector_defaults:
              type: rocketchat
              server: {url: http://localhost:3000, username: bot, password: pw}
            connectors:
              - name: rc1
              - name: rc2
                server: {username: bot2}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        rc1, rc2 = config.connectors[0], config.connectors[1]
        self.assertEqual(rc1.type, "rocketchat")
        self.assertEqual(rc1.raw["server"]["username"], "bot")
        # rc2 overrides only username; url/password still inherited from defaults.
        self.assertEqual(rc2.raw["server"]["username"], "bot2")
        self.assertEqual(rc2.raw["server"]["url"], "http://localhost:3000")
        self.assertEqual(rc2.raw["server"]["password"], "pw")

    def test_connector_defaults_forbids_name(self):
        path = self._write_config("""\
            connector_defaults:
              name: not-allowed
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
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
        self.assertIn("connector_defaults", str(ctx.exception))
        self.assertIn("name", str(ctx.exception))

    def test_connector_defaults_must_be_mapping(self):
        path = self._write_config("""\
            connector_defaults: not-a-mapping
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
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
        self.assertIn("connector_defaults", str(ctx.exception))

    def test_attachments_cache_dir_global_default_not_aliased_across_connectors(self):
        """Regression: cache_dir_global resolution mutates the connector's raw dict
        in place. Without a deep-copying merge, two connectors sharing
        connector_defaults.attachments would alias the same nested dict, and
        resolving/mutating it for the first connector would corrupt the second."""
        path = self._write_config("""\
            connector_defaults:
              type: rocketchat
              server: {url: http://localhost:3000, username: bot, password: pw}
              attachments:
                cache_dir_global: shared-cache
            connectors:
              - name: rc1
              - name: rc2
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        a = config.connectors[0].raw["attachments"]
        b = config.connectors[1].raw["attachments"]
        self.assertIsNot(a, b)
        self.assertEqual(a["cache_dir_global"], b["cache_dir_global"])
        a["cache_dir_global"] = "mutated"
        self.assertNotEqual(b["cache_dir_global"], "mutated")


class TestAgentDefaults(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_agent_inherits_defaults(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_defaults:
              type: claude
              working_directory: /tmp
              timeout: 500
            agents:
              default: {}
              other:
                timeout: 42
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")
        self.assertEqual(config.agents["default"].timeout, 500)
        # 'other' overrides timeout but still inherits working_directory/type.
        self.assertEqual(config.agents["other"].timeout, 42)
        self.assertEqual(config.agents["other"].working_directory, "/tmp")

    def test_agent_defaults_permissions_deep_merge(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_defaults:
              type: claude
              working_directory: /tmp
              timeout: 500
              permissions: {enabled: true, timeout: 300}
            agents:
              default:
                permissions: {timeout: 100}
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        perms = config.agents["default"].permissions
        # enabled inherited from defaults, timeout overridden by the entry.
        self.assertTrue(perms.enabled)
        self.assertEqual(perms.timeout, 100)

    def test_agent_defaults_tool_list_replaces_not_appends(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_defaults:
              type: claude
              working_directory: /tmp
              owner_allowed_tools:
                - tool: Read
                - tool: Grep
            agents:
              default:
                owner_allowed_tools:
                  - tool: Write
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Write"])


class TestWatcherDefaults(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_watcher_inherits_connector_and_agent(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_defaults:
              connector: rc
              agent: default
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        wc = config.watchers[0]
        self.assertEqual(wc.connector, "rc")
        self.assertEqual(wc.agent, "default")

    def test_watcher_defaults_forbids_identity_fields(self):
        for key, value in (
            ("name", "shared-name"),
            ("room", "general"),
            ("rooms", ["general"]),
            ("session_id", "sticky-1"),
        ):
            with self.subTest(key=key):
                path = self._write_config(f"""\
                    connectors:
                      - name: rc
                        type: rocketchat
                        server: {{url: http://localhost:3000, username: bot, password: pw}}
                    agents:
                      default:
                        type: claude
                        working_directory: /tmp
                    watcher_defaults:
                      {key}: {value!r}
                    watchers:
                      - name: w1
                        room: general
                """)
                with self.assertRaises(ValueError) as ctx:
                    GatewayConfig.from_file(path)
                self.assertIn("watcher_defaults", str(ctx.exception))
                self.assertIn(key, str(ctx.exception))


# ── Tests: tool_presets ────────────────────────────────────────────────────────


class TestToolPresets(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_preset_reference_resolves_to_rules(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              readonly:
                - tool: Read
                - tool: Grep
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - readonly
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Read", "Grep"])

    def test_preset_and_inline_rules_are_mixable_and_ordered(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              readonly:
                - tool: Read
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - tool: Bash
                    params: "git .*"
                  - readonly
                  - tool: Write
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Bash", "Read", "Write"])

    def test_unknown_preset_reference_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - nonexistent
            watchers:
              - name: w1
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("unknown tool preset 'nonexistent'", str(ctx.exception))

    def test_preset_referencing_another_preset_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              base:
                - tool: Read
              wrapper:
                - base
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
        self.assertIn("presets cannot reference another preset", str(ctx.exception))

    def test_invalid_preset_rule_raises_even_if_unused(self):
        """Presets are validated eagerly at load, even if no agent references them."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              broken:
                - tool: '[invalid'
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
        self.assertIn("invalid tool rule", str(ctx.exception).lower())

    def test_invalid_inline_rule_raises_with_agent_and_field_context(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                guest_allowed_tools:
                  - tool: '[invalid'
            watchers:
              - name: w1
                room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("invalid tool rule", msg.lower())
        self.assertIn("guest_allowed_tools", msg)


# ── Tests: watcher rooms: expansion + auto-naming ─────────────────────────────


class TestWatcherRoomsExpansion(unittest.TestCase):
    def _write_config(self, watchers_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc-home
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
{textwrap.indent(textwrap.dedent(watchers_block), "              ")}
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_rooms_list_expands_to_one_watcher_per_room(self):
        path = self._write_config("""\
            - connector: rc-home
              rooms: [general, dev, '@alice']
        """)
        config = GatewayConfig.from_file(path)
        names = {w.name: w.room for w in config.watchers}
        self.assertEqual(
            names,
            {
                "rc-home-general": "general",
                "rc-home-dev": "dev",
                "rc-home-dm-alice": "@alice",
            },
        )

    def test_room_singular_is_alias_for_single_item_rooms(self):
        path = self._write_config("""\
            - connector: rc-home
              room: general
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(len(config.watchers), 1)
        self.assertEqual(config.watchers[0].name, "rc-home-general")
        self.assertEqual(config.watchers[0].room, "general")

    def test_room_and_rooms_both_set_raises(self):
        path = self._write_config("""\
            - connector: rc-home
              room: general
              rooms: [dev]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("set either 'room' or 'rooms', not both", str(ctx.exception))

    def test_rooms_must_be_non_empty_list(self):
        path = self._write_config("""\
            - connector: rc-home
              rooms: []
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("'rooms' must be a non-empty list", str(ctx.exception))

    def test_rooms_with_duplicate_room_raises(self):
        path = self._write_config("""\
            - connector: rc-home
              rooms: [general, general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("duplicate room(s)", str(ctx.exception))

    def test_explicit_name_with_multiple_rooms_raises(self):
        path = self._write_config("""\
            - connector: rc-home
              name: my-watcher
              rooms: [general, dev]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("'name' can only be set when there is exactly one room", str(ctx.exception))

    def test_explicit_session_id_with_multiple_rooms_raises(self):
        path = self._write_config("""\
            - connector: rc-home
              session_id: sticky-1
              rooms: [general, dev]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn(
            "'session_id' can only be set when there is exactly one room",
            str(ctx.exception),
        )

    def test_explicit_name_preserved_on_single_room_entry(self):
        path = self._write_config("""\
            - connector: rc-home
              name: general-room
              room: ops
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.watchers[0].name, "general-room")

    def test_auto_name_collision_across_entries_raises(self):
        path = self._write_config("""\
            - connector: rc-home
              room: general
            - connector: rc-home
              room: general
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("Duplicate watcher name 'rc-home-general'", msg)
        self.assertIn("set an explicit 'name:' to disambiguate", msg)

    def test_room_sanitization_examples(self):
        from gateway.config import _auto_watcher_name

        for room, expected_fragment in (
            ("general", "general"),
            ("@alice", "dm-alice"),
            ("team/town-square", "team-town-square"),
        ):
            with self.subTest(room=room):
                self.assertEqual(
                    _auto_watcher_name("mm", room), f"mm-{expected_fragment}"
                )


# ── Tests: quiet notification defaults ────────────────────────────────────────


class TestQuietNotificationDefaults(unittest.TestCase):
    """Migration-0.2 behavior change: online/offline notifications default to
    quiet (None) instead of posting '_Agent online_'/'_Agent offline_'."""

    def _write_config(self, watcher_extra: str = "") -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
{textwrap.indent(textwrap.dedent(watcher_extra), "                ")}
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_notifications_default_to_none(self):
        path = self._write_config()
        config = GatewayConfig.from_file(path)
        self.assertIsNone(config.watchers[0].online_notification)
        self.assertIsNone(config.watchers[0].offline_notification)

    def test_watcher_defaults_can_restore_old_behavior_globally(self):
        path = self._write_config()
        # Inject watcher_defaults restoring the pre-0.2 notification text.
        with open(path) as f:
            body = f.read()
        body = body.replace(
            "connectors:",
            "watcher_defaults:\n"
            "  online_notification: '✅ _Agent online_'\n"
            "  offline_notification: '❌ _Agent offline_'\n"
            "connectors:",
            1,
        )
        with open(path, "w") as f:
            f.write(body)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.watchers[0].online_notification, "✅ _Agent online_")
        self.assertEqual(config.watchers[0].offline_notification, "❌ _Agent offline_")

    def test_explicit_notification_overrides_default(self):
        path = self._write_config(
            "online_notification: 'hi there'\noffline_notification: 'bye'"
        )
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.watchers[0].online_notification, "hi there")
        self.assertEqual(config.watchers[0].offline_notification, "bye")


# ── Tests: description: field (informational only, ignored at runtime) ──────


class TestDescriptionField(unittest.TestCase):
    """'description:' is a purely informational annotation (read by the
    config TUI) — the loader must accept it everywhere without letting it
    affect behavior: it must not appear in a connector's raw dict, and a
    *_defaults block's own description must never propagate into entries."""

    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_connector_description_is_not_in_raw(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                description: "Primary bot account"
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        self.assertNotIn("description", config.connectors[0].raw)

    def test_connector_defaults_description_does_not_propagate(self):
        path = self._write_config("""\
            connector_defaults:
              description: "Shared settings for all bots"
              type: rocketchat
              server: {url: http://localhost:3000, username: bot, password: pw}
            connectors:
              - name: rc1
              - name: rc2
                description: "This one's special"
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        config = GatewayConfig.from_file(path)
        # Neither connector's raw should carry a 'description' key — rc1 never
        # had one of its own, and rc2's own value must not have been merged
        # with (or overwritten by) the defaults block's description.
        self.assertNotIn("description", config.connectors[0].raw)
        self.assertNotIn("description", config.connectors[1].raw)

    def test_agent_and_watcher_description_do_not_break_loading(self):
        """agents/watchers already ignore unknown keys via .get() — this just
        pins that 'description' specifically doesn't need special handling
        there and doesn't leak into any typed field."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_defaults:
              description: "Shared claude settings"
            agents:
              default:
                type: claude
                working_directory: /tmp
                description: "The main agent"
            watcher_defaults:
              description: "Shared watcher settings"
            watchers:
              - name: w1
                room: general
                description: "General channel watcher"
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")
        self.assertEqual(len(config.watchers), 1)
        self.assertEqual(config.watchers[0].name, "w1")


if __name__ == "__main__":
    unittest.main()
