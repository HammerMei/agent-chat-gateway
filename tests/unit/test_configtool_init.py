"""Unit tests for gateway/configtool/__init__.py's run_app() — specifically
the .env auto-migration trigger added ahead of launching the config TUI.

`ConfigToolApp.run()` is mocked out everywhere (it would otherwise try to
take over a real terminal) — these tests only cover the pre-launch logic:
the migration call, its success/no-op/error handling, and the tty guard.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from gateway.config_migrate import MigrationResult
from gateway.configtool import run_app


class TestRunAppMigratesBeforeLaunch(unittest.TestCase):
    def setUp(self):
        self.isatty_patch = patch("sys.stdin.isatty", return_value=True)
        self.isatty_out_patch = patch("sys.stdout.isatty", return_value=True)
        self.isatty_patch.start()
        self.isatty_out_patch.start()
        self.addCleanup(self.isatty_patch.stop)
        self.addCleanup(self.isatty_out_patch.stop)

    def test_calls_migrate_env_to_config_before_constructing_the_app(self):
        with (
            patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate,
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
        ):
            mock_migrate.return_value = MigrationResult(migrated=False)
            mock_app_cls.return_value = MagicMock()

            run_app("some/config.yaml")

            mock_migrate.assert_called_once_with("some/config.yaml")
            mock_app_cls.return_value.run.assert_called_once()

    def test_prints_a_summary_when_a_migration_actually_happened(self):
        with (
            patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate,
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
            patch("builtins.print") as mock_print,
        ):
            mock_migrate.return_value = MigrationResult(
                migrated=True, ref_count=2, env_backup_path="/cfg/.config-backups/.env.bak.1"
            )
            mock_app_cls.return_value = MagicMock()

            code = run_app("some/config.yaml")

            self.assertEqual(code, 0)
            mock_app_cls.return_value.run.assert_called_once()
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
            self.assertIn("Migrated 2 secret reference(s)", printed)

    def test_says_nothing_extra_when_there_was_nothing_to_migrate(self):
        with (
            patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate,
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
            patch("builtins.print") as mock_print,
        ):
            mock_migrate.return_value = MigrationResult(migrated=False)
            mock_app_cls.return_value = MagicMock()

            run_app("some/config.yaml")

            mock_app_cls.return_value.run.assert_called_once()
            mock_print.assert_not_called()

    def test_missing_config_file_does_not_block_the_tui_from_opening(self):
        """The TUI has its own graceful 'does not currently load' banner for
        a missing/broken config — migrate_env_to_config()'s FileNotFoundError
        must not prevent ConfigToolApp from being constructed at all."""
        with (
            patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate,
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
        ):
            mock_migrate.side_effect = FileNotFoundError("Config file not found: x")
            mock_app_cls.return_value = MagicMock()

            code = run_app("does-not-exist.yaml")

            self.assertEqual(code, 0)
            mock_app_cls.return_value.run.assert_called_once()

    def test_unresolvable_env_var_during_migration_blocks_launch_with_a_clean_error(self):
        with (
            patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate,
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
        ):
            mock_migrate.side_effect = ValueError("Unresolved environment variable: FOO")
            mock_app_cls.return_value = MagicMock()

            code = run_app("some/config.yaml")

            self.assertEqual(code, 1)
            mock_app_cls.return_value.run.assert_not_called()

    def test_non_interactive_terminal_is_rejected_before_migration_even_runs(self):
        self.isatty_patch.stop()
        self.isatty_patch = patch("sys.stdin.isatty", return_value=False)
        self.isatty_patch.start()

        with patch("gateway.config_migrate.migrate_env_to_config") as mock_migrate:
            code = run_app("some/config.yaml")

            self.assertEqual(code, 1)
            mock_migrate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
