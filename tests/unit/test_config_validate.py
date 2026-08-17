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
from gateway.core.state import STATE_FORMAT_VERSION


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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            # Version marker included deliberately rather than hardcoded: a file
            # without one is now refused, and this fixture is a *current* file.
            "version": STATE_FORMAT_VERSION,
            "watchers": [
                # A rule-derived record is never an orphan (§2.4): its
                # recreation source is the record, not a config entry.
                {"watcher_name": "w1", "session_id": "keep", "room_id": "r1",
                 "rule_name": "w1"},
                # A static-era record (no rule_name) is pruned at next start.
                {"watcher_name": "stale", "session_id": "x", "room_id": "r2"},
            ]
        }))
        result = self._validate(cfg)
        self.assertTrue(result.ok)  # orphans are warnings, not errors
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("stale", result.warnings[0])
        self.assertIn("static-era", result.warnings[0])

    def test_a_state_file_of_a_removed_connector_is_reported(self):
        """Codex round 4: a connector renamed or removed in config.yaml leaves
        its state file behind, and no SessionManager will ever hydrate it —
        without this warning its records (and their sessions) are abandoned
        silently. Rule-derived records are exactly the ones this matters for:
        they are 'never an orphan' by shape, so the connector-set comparison
        is the only check that can catch them."""
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.old-rc.json").write_text(json.dumps({
            "version": STATE_FORMAT_VERSION,
            "watchers": [
                {"watcher_name": "w1", "session_id": "keep", "room_id": "r1",
                 "rule_name": "w1"},
            ]
        }))
        result = self._validate(cfg)
        self.assertTrue(result.ok, "abandoned records are a warning, not an error")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("old-rc", result.warnings[0])
        self.assertIn("not in config.yaml", result.warnings[0])

    def test_an_empty_leftover_state_file_is_not_reported(self):
        """The noise gate: a removed connector's file with no records has
        nothing to abandon."""
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.old-rc.json").write_text(json.dumps({
            "version": STATE_FORMAT_VERSION, "watchers": []
        }))
        result = self._validate(cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.warnings, [])

    def test_a_legacy_state_file_is_reported_as_an_error_not_skipped(self):
        """The branch that reads state used to be `except Exception: continue`.

        That would have swallowed the legacy-format refusal completely — and this
        command is the first thing an upgrading operator runs, so it would have
        reported a clean config while the daemon refused to boot on the same files.
        Reported as an *error* rather than a warning, because the gateway will not
        start until it is dealt with.
        """
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        # No version marker — the shape written by every earlier release.
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            "watchers": [{"watcher_name": "w1", "session_id": "s", "room_id": "r"}]
        }))
        result = self._validate(cfg)
        self.assertFalse(result.ok, "a refused state file must not validate clean")
        self.assertTrue(
            any("state.rc.json" in e for e in result.errors), result.errors
        )
        # Attributed to the file, not to a connector: the check now enumerates state
        # files rather than configured connectors, precisely because a file may belong
        # to a connector that no longer exists in config.yaml. "global" is the honest
        # entity for something the config does not mention.
        findings = [
            f for f in result.findings
            if f.severity == "error" and f.entity_kind == "global"
        ]
        self.assertTrue(findings, result.findings)
        self.assertIn("§5.3", findings[0].message)

    def test_a_state_file_for_an_unconfigured_connector_is_reported_too(self):
        """The hole this restructure closed: iterating `config.connectors` would never
        open `state.retired.json`, so its sessions would be abandoned by a boot that
        reported success."""
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.retired.json").write_text(json.dumps({
            "watchers": [{"watcher_name": "gone", "session_id": "s", "room_id": "r"}]
        }))
        result = self._validate(cfg)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("state.retired.json" in e for e in result.errors), result.errors
        )

    def test_a_corrupt_state_file_still_validates_clean(self):
        """The contrast that keeps the error above meaningful: a corrupt file is
        handled by starting fresh, so it is not a validation error."""
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text("{ not json")
        result = self._validate(cfg)
        self.assertTrue(result.ok, result.errors)

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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
                description: "General channel"
        """)
        result = self._validate(cfg, lint=True)
        joined = " ".join(result.lint_findings)
        self.assertNotIn("description", joined)

    def test_lint_does_not_crash_when_a_templates_block_itself_is_malformed(self):
        """PR review finding: _lint_config() used to assume re-parsing
        agent_templates/watcher_templates/connector_templates "cannot
        raise" since it only ran after a fully successful load. That
        stopped being true once validate_config() switched to
        collect_config() (fault-tolerant) — a malformed `agent_templates:`
        block is caught and reported as its own error by collect_config(),
        but connectors/agents/watchers can still parse fine independently.
        --lint must not re-raise that same error uncaught."""
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agent_templates: not-a-mapping
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                rooms:
                  include: [general]
        """)
        result = self._validate(cfg, lint=True)  # must not raise
        self.assertFalse(result.ok)
        self.assertTrue(any("agent_templates" in e for e in result.errors))

    def test_lint_does_not_attach_a_non_string_connector_name_to_a_finding(self):
        """PR review finding: _lint_config()'s `name = cc.get("name") or
        "?"` used the raw value verbatim — a truthy-but-non-string name
        (e.g. a YAML list) reached Finding.entity_name (typed str | None)
        unchecked. validate_config() itself doesn't crash on this (dataclass
        construction doesn't type-check), but the config TUI's StatusIndex
        does, the first time it tries to use (entity_kind, entity_name) as a
        dict key (see test_configtool_model.py's
        TestStatusIndexNonStringEntityName for the actual crash repro) —
        pinned here at the source instead: every Finding this produces must
        have a STRING entity_name, never the raw malformed value. Requires
        'reply_in_thread: false' below (restating a built-in default) so
        _lint_entry() actually appends a Finding for this connector at all —
        without a lint-worthy field, entity_name is never even read."""
        cfg = self._write(f"""\
            connectors:
              - name: [a, b]
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
                reply_in_thread: false
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        result = self._validate(cfg, lint=True)  # must not raise
        connector_findings = [f for f in result.findings if f.entity_kind == "connector"]
        self.assertTrue(connector_findings)
        for f in connector_findings:
            self.assertIsInstance(f.entity_name, str)

    def test_lint_does_not_attach_a_non_string_watcher_name_to_a_finding(self):
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
              - name: [a, b]
                connector: rc
                rooms:
                  include: [general]
                online_notification: null
        """)
        result = self._validate(cfg, lint=True)  # must not raise
        watcher_findings = [f for f in result.findings if f.entity_kind == "watcher"]
        self.assertTrue(watcher_findings)
        for f in watcher_findings:
            self.assertIsInstance(f.entity_name, str)

    def test_lint_does_not_crash_on_a_non_hashable_inherits_value(self):
        """PR review finding: _lint_entry()'s `templates.get(template_name,
        {})` requires template_name to be hashable — a malformed
        'inherits:' (e.g. a YAML list) raised an uncaught
        TypeError: unhashable type straight out of --lint, aborting the
        whole pass and discarding every already-collected finding."""
        cfg = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                inherits: [a, b]
        """)
        result = self._validate(cfg, lint=True)  # must not raise
        self.assertFalse(result.ok)

    def test_lint_does_not_crash_when_agents_block_is_not_a_mapping(self):
        """PR review finding: `(raw.get("agents") or {}).items()` assumed
        `agents:` is always a dict by the time --lint runs — already-false
        for collect_config()'s own structural check, which reports this as
        a clean issue elsewhere and returns a partial config with agents={}
        rather than raising. --lint must not re-crash on the same raw,
        unvalidated value with an uncaught AttributeError."""
        cfg = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents: [1, 2, 3]
        """)
        result = self._validate(cfg, lint=True)  # must not raise
        self.assertFalse(result.ok)


class TestFindingsExtension(_ValidateConfigTestBase):
    """`findings: list[Finding]` is additive alongside the flat string lists —
    every append to errors/warnings/lint_findings must have a matching
    Finding, and the flat lists (CLI output) must stay unaffected."""

    def test_agent_load_failure_is_attributed_to_that_agent_not_global(self):
        """Fault-tolerant collect_config() (gateway/config.py), not a single
        caught GatewayConfig.from_file() exception: a per-agent problem is
        now attributed to entity_kind="agent"/entity_name="default", not
        entity_kind="global" — this is what lets the config TUI's Overview
        mark the RIGHT row, instead of a global banner nothing points at.
        This specific fixture's only agent fails, so `agents` ends up empty
        too — a second, genuinely global finding ("must define at least one
        agent") is expected alongside the per-agent one; both are real and
        independently true."""
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
                rooms:
                  include: [general]
        """)
        result = self._validate(cfg)
        self.assertEqual(len(result.findings), 2)
        agent_finding = next(f for f in result.findings if f.entity_kind == "agent")
        self.assertEqual(agent_finding.severity, "error")
        self.assertEqual(agent_finding.entity_name, "default")
        self.assertIn("working_directory is required", agent_finding.message)
        global_finding = next(f for f in result.findings if f.entity_kind == "global")
        self.assertIsNone(global_finding.entity_name)
        self.assertIn("must define at least one agent", global_finding.message)

    def test_two_independently_broken_agents_both_surface(self):
        """The actual scenario this whole change exists for: TWO agents each
        independently missing working_directory both produce their own
        Finding in one pass — not just whichever one from_file() would have
        hit first."""
        cfg = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              agent1:
                type: claude
              agent2:
                type: claude
            watchers:
              - connector: rc
                agent: agent1
                rooms:
                  include: [general]
        """)
        result = self._validate(cfg)
        agent_findings = {
            f.entity_name: f for f in result.findings if f.entity_kind == "agent"
        }
        self.assertEqual(set(agent_findings), {"agent1", "agent2"})
        for f in agent_findings.values():
            self.assertIn("working_directory is required", f.message)

    def test_two_independently_broken_connectors_both_surface_structurally(self):
        """Same as the agent case, but for a structural (not credential-
        level) connector problem — each missing 'type' independently."""
        cfg = self._write(f"""\
            connectors:
              - name: conn1
              - name: conn2
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        result = self._validate(cfg)
        connector_findings = {
            f.entity_name: f for f in result.findings if f.entity_kind == "connector"
        }
        self.assertEqual(set(connector_findings), {"conn1", "conn2"})
        for f in connector_findings.values():
            self.assertIn("must have a 'type' field", f.message)

    def test_malformed_top_level_structure_is_still_one_global_finding(self):
        """A genuinely structural break (here: 'connectors:' is a mapping,
        not a list) has no per-entity fallback — collect_config() can't
        partially parse "some" connectors when the whole block's shape is
        wrong, so this must still be exactly one global finding, same as
        before this change."""
        cfg = self._write("""\
            connectors: {not: a-list}
            agents:
              default:
                type: claude
        """)
        result = self._validate(cfg)
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.severity, "error")
        self.assertEqual(finding.entity_kind, "global")
        self.assertIsNone(finding.entity_name)
        self.assertIn("'connectors:' must be a list", finding.message)

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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            "version": STATE_FORMAT_VERSION,
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
                rooms:
                  include: [general]
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
