"""Unit tests for gateway/config_migrate.py — the one-time .env -> config.yaml
migration (docs/design/config-tool.md decision 6 revisited).

Covers the migration LOGIC only; gateway/daemon.py's auto-invocation at
startup and the CLI entry point are covered separately.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from gateway.config_migrate import migrate_env_to_config


class TestMigrateEnvToConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.config_path = Path(self.tmp) / "config.yaml"
        self.env_path = Path(self.tmp) / ".env"

    def _write_config(self, yaml_text: str) -> None:
        self.config_path.write_text(textwrap.dedent(yaml_text))

    def _valid_cfg_text(self, password: str) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: "{password}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
        """

    def test_no_env_file_is_a_no_op(self):
        self._write_config(self._valid_cfg_text("plaintext-already"))
        result = migrate_env_to_config(self.config_path)
        self.assertFalse(result.migrated)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "plaintext-already")

    def test_resolves_the_reference_and_writes_the_literal_value(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        self.assertEqual(result.ref_count, 1)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")

    def test_env_file_is_moved_into_config_backups_not_left_in_place(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        self.assertFalse(self.env_path.exists())
        self.assertIsNotNone(result.env_backup_path)
        self.assertTrue(result.env_backup_path.exists())
        self.assertEqual(result.env_backup_path.read_text(), "RC_PASSWORD=hunter2\n")
        self.assertEqual(result.env_backup_path.parent.name, ".config-backups")

    def test_backup_and_directory_are_permissioned(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        backup_dir = result.env_backup_path.parent
        self.assertEqual(oct(backup_dir.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(result.env_backup_path.stat().st_mode)[-3:], "600")

    def test_config_yaml_backup_is_also_created_by_the_reused_save_path(self):
        """migrate_env_to_config() must not reimplement EditableConfig.
        save()'s own backup step — just exercise it. Tolerant of either
        backup location/convention EditableConfig.save() itself uses (that
        detail is EditableConfig's own to test, in test_configtool_model.py)."""
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        migrate_env_to_config(self.config_path)

        flat = list(self.config_path.parent.glob("config.yaml.bak.*"))
        nested = list((self.config_path.parent / ".config-backups").glob("config.yaml.bak.*"))
        self.assertEqual(len(flat) + len(nested), 1)

    def test_unresolvable_reference_raises_and_leaves_everything_untouched(self):
        self._write_config(self._valid_cfg_text("${MISSING_VAR}"))
        self.env_path.write_text("SOME_OTHER_VAR=irrelevant\n")
        original_config_text = self.config_path.read_text()
        original_env_text = self.env_path.read_text()

        with self.assertRaises(ValueError):
            migrate_env_to_config(self.config_path)

        self.assertEqual(self.config_path.read_text(), original_config_text)
        self.assertTrue(self.env_path.exists())
        self.assertEqual(self.env_path.read_text(), original_env_text)

    def test_resolves_from_ambient_environment_when_not_in_env_file(self):
        """load_dotenv() doesn't override an already-set process env var —
        matching GatewayConfig.from_file's own resolution order — so a
        reference the daemon has always resolved from the ambient
        environment (not .env) migrates to that SAME literal value, not an
        unresolved error."""
        self._write_config(self._valid_cfg_text("${RC_AMBIENT_TEST_VAR}"))
        # .env exists (so the migration triggers) but doesn't define this var.
        self.env_path.write_text("UNRELATED=1\n")
        os.environ["RC_AMBIENT_TEST_VAR"] = "from-the-shell"
        self.addCleanup(os.environ.pop, "RC_AMBIENT_TEST_VAR", None)

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "from-the-shell")

    def test_migrates_multiple_references_across_different_fields(self):
        text = f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "${{RC_URL}}", username: "${{RC_USER}}", password: "${{RC_PASSWORD}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
        """
        self._write_config(text)
        self.env_path.write_text(
            "RC_URL=http://chat.example.com\nRC_USER=bot\nRC_PASSWORD=hunter2\n"
        )

        result = migrate_env_to_config(self.config_path)

        self.assertEqual(result.ref_count, 3)
        raw = yaml.safe_load(self.config_path.read_text())
        server = raw["connectors"][0]["server"]
        self.assertEqual(server["url"], "http://chat.example.com")
        self.assertEqual(server["username"], "bot")
        self.assertEqual(server["password"], "hunter2")

    def test_ref_count_is_zero_when_env_exists_but_nothing_references_it(self):
        """.env can exist without config.yaml referencing anything in it
        (e.g. leftover from a prior manual setup) — still migrates (removes
        the now-pointless .env), just with nothing to report."""
        self._write_config(self._valid_cfg_text("plaintext-already"))
        self.env_path.write_text("UNUSED_VAR=whatever\n")

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        self.assertEqual(result.ref_count, 0)
        self.assertFalse(self.env_path.exists())


if __name__ == "__main__":
    unittest.main()
