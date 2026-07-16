"""Unit tests for gateway/config_validate.py — the standalone (no-daemon)
config validation used by `acg config validate`.

CLI-level coverage (argument parsing, output formatting, exit codes) lives in
tests/integration/test_cli.py::TestCLIConfigValidate. These tests exercise
validate_config() directly.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.config_validate import validate_config


class _ValidateConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.runtime_dir = Path(self.tmp) / "runtime"

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)

    def _validate(self, config_path: str, lint: bool = False):
        with patch("gateway.core.state.RUNTIME_DIR", self.runtime_dir):
            return validate_config(config_path, lint=lint)


class TestValidateConfigBasics(_ValidateConfigTestBase):
    def test_valid_config_has_no_errors(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.watcher_count, 1)
        self.assertEqual(result.entry_count, 1)

    def test_nonexistent_file_is_an_error(self):
        result = self._validate("/nonexistent/config.yaml")
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)

    def test_from_file_error_is_surfaced_verbatim(self):
        cfg = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertFalse(result.ok)
        self.assertTrue(any("working_directory is required" in e for e in result.errors))


class TestValidateConfigConnectorChecks(_ValidateConfigTestBase):
    def test_empty_rocketchat_server_fields_are_errors(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertFalse(result.ok)
        joined = " ".join(result.errors)
        self.assertIn("server.url is empty", joined)
        self.assertIn("server.username is empty", joined)
        self.assertIn("server.password is empty", joined)

    def test_mattermost_missing_auth_mode_is_an_error(self):
        """MattermostConfig.__post_init__ already raises when neither token
        nor username+password is set — config_validate must surface that."""
        cfg = self._write(f"""\
            connectors:
              - name: mm
                type: mattermost
                server: {{url: http://localhost:8065, team: home}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertFalse(result.ok)
        self.assertTrue(any("mm" in e for e in result.errors))

    def test_script_connector_is_not_validated(self):
        """ScriptConnector never reads ConnectorConfig.raw — nothing to check."""
        cfg = self._write(f"""\
            connectors:
              - name: sc
                type: script
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertTrue(result.ok)


class TestValidateConfigStateOrphans(_ValidateConfigTestBase):
    def test_orphaned_state_watcher_produces_warning(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            "watchers": [
                {"watcher_name": "w1", "session_id": "keep", "room_id": "r1"},
                {"watcher_name": "stale", "session_id": "x", "room_id": "r2"},
            ]
        }))
        result = self._validate(cfg)
        self.assertTrue(result.ok)  # orphans are warnings, not errors
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("stale", result.warnings[0])

    def test_no_state_file_produces_no_warnings(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        self.assertEqual(result.warnings, [])


class TestValidateConfigLint(_ValidateConfigTestBase):
    def test_lint_off_by_default(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 360
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=False)
        self.assertEqual(result.lint_findings, [])

    def test_lint_flags_agent_field_matching_builtin_default(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 360
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=True)
        self.assertTrue(
            any("agents.default.timeout" in f for f in result.lint_findings)
        )

    def test_lint_flags_entry_matching_agent_defaults(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agent_defaults:
              type: claude
              working_directory: {self.agent_dir}
              timeout: 500
            agents:
              default:
                timeout: 500
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=True)
        self.assertTrue(
            any(
                "agents.default.timeout" in f and "agent_defaults" in f
                for f in result.lint_findings
            )
        )

    def test_lint_does_not_flag_deliberate_override(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agent_defaults:
              type: claude
              working_directory: {self.agent_dir}
              timeout: 500
            agents:
              default:
                timeout: 999
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=True)
        self.assertEqual(
            [f for f in result.lint_findings if "agents.default.timeout" in f], []
        )

    def test_lint_flags_connector_attachment_defaults(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
                attachments:
                  max_file_size_mb: 10
                  download_timeout: 30
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=True)
        joined = " ".join(result.lint_findings)
        self.assertIn("max_file_size_mb", joined)
        self.assertIn("download_timeout", joined)


if __name__ == "__main__":
    unittest.main()
