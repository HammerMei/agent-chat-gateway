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

from gateway.config_validate import Finding, validate_config


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

    def test_malformed_rocketchat_url_is_an_error(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "test", username: bot, password: pw}}
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
        self.assertTrue(
            any("server.url" in e and "does not look like a URL" in e for e in result.errors)
        )

    def test_malformed_mattermost_url_is_an_error(self):
        cfg = self._write(f"""\
            connectors:
              - name: mm
                type: mattermost
                server: {{url: "localhost:8065", team: home, token: tok}}
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
        self.assertTrue(
            any("server.url" in e and "does not look like a URL" in e for e in result.errors)
        )

    def test_well_formed_url_with_uncommon_scheme_is_not_flagged(self):
        """Lenient check: only scheme+netloc are required — an unusual but
        well-formed scheme is not second-guessed."""
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "wss://localhost:3000", username: bot, password: pw}}
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

    def test_empty_url_produces_only_the_empty_field_error_not_a_url_error(self):
        """An empty server.url must not additionally be flagged as malformed
        — that would be a confusing, redundant double-error for one field."""
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        url_errors = [e for e in result.errors if "server.url" in e]
        self.assertEqual(len(url_errors), 1)
        self.assertIn("is empty", url_errors[0])

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

    def test_lint_flags_entry_matching_agent_template(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 500
            agents:
              default:
                inherits: standard
                timeout: 500
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg, lint=True)
        self.assertTrue(
            any(
                "agents.default.timeout" in f and "agent_templates" in f
                for f in result.lint_findings
            )
        )

    def test_lint_does_not_flag_deliberate_override(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 500
            agents:
              default:
                inherits: standard
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

    def test_lint_never_flags_description(self):
        """'description:' is a free-text annotation, not a default-restating
        field — --lint must never mention it, however it's set."""
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                description: "Primary bot"
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                description: "The main agent"
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
                description: "General channel"
        """)
        result = self._validate(cfg, lint=True)
        joined = " ".join(result.lint_findings)
        self.assertNotIn("description", joined)


class TestFindingsExtension(_ValidateConfigTestBase):
    """`findings: list[Finding]` is additive alongside the flat string lists —
    every append to errors/warnings/lint_findings must have a matching
    Finding, and the flat lists (CLI output) must stay unaffected."""

    def test_load_failure_produces_one_global_finding(self):
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
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.severity, "error")
        self.assertEqual(finding.entity_kind, "global")
        self.assertIsNone(finding.entity_name)
        self.assertIn("working_directory is required", finding.message)

    def test_empty_connector_credentials_produce_per_field_findings(self):
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
        connector_findings = {f.field: f for f in result.findings if f.entity_kind == "connector"}
        self.assertEqual(connector_findings.keys(), {"server.url", "server.username", "server.password"})
        for f in connector_findings.values():
            self.assertEqual(f.severity, "error")
            self.assertEqual(f.entity_name, "rc")

    def test_malformed_url_produces_a_per_field_finding(self):
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "test", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        result = self._validate(cfg)
        connector_findings = {f.field: f for f in result.findings if f.entity_kind == "connector"}
        self.assertEqual(connector_findings.keys(), {"server.url"})
        finding = connector_findings["server.url"]
        self.assertEqual(finding.severity, "error")
        self.assertEqual(finding.entity_name, "rc")
        self.assertIn("does not look like a URL", finding.message)

    def test_state_orphan_produces_warning_finding(self):
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
            "watchers": [{"watcher_name": "stale", "session_id": "x", "room_id": "y"}]
        }))
        result = self._validate(cfg)
        warning_findings = [f for f in result.findings if f.severity == "warning"]
        self.assertEqual(len(warning_findings), 1)
        self.assertEqual(warning_findings[0].entity_kind, "connector")
        self.assertEqual(warning_findings[0].entity_name, "rc")
        self.assertIsNone(warning_findings[0].field)

    def test_lint_findings_are_attributed_per_entity_and_field(self):
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
        lint_findings = [f for f in result.findings if f.severity == "lint"]
        self.assertTrue(
            any(
                f.entity_kind == "agent" and f.entity_name == "default" and f.field == "timeout"
                for f in lint_findings
            )
        )

    def test_findings_never_present_when_config_is_clean(self):
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
        self.assertEqual(result.findings, [])

    def test_second_read_oserror_produces_a_matching_finding(self):
        """Regression: the OSError branch (a second, independent re-read of
        config.yaml purely to compute entry_count) used to append to
        result.errors without a matching Finding — the one error-append site
        in this file that didn't. Patching gateway.config_validate's own
        `open` (not gateway.config's) isolates the failure to just that
        second read; GatewayConfig.from_file's own read succeeds normally."""
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
        with patch("gateway.config_validate.open", side_effect=OSError("boom")):
            result = self._validate(cfg)

        self.assertFalse(result.ok)
        self.assertTrue(any("Could not re-read" in e for e in result.errors))
        matching = [
            f for f in result.findings
            if f.entity_kind == "global" and "Could not re-read" in f.message
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "error")

    def test_finding_is_a_frozen_dataclass_instance(self):
        f = Finding(
            severity="error", entity_kind="connector", entity_name="rc",
            field="server.url", message="server.url is empty",
        )
        self.assertEqual(f.severity, "error")
        with self.assertRaises(Exception):
            f.severity = "warning"  # frozen — must not be mutable


if __name__ == "__main__":
    unittest.main()
