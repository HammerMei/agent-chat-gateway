"""Unit tests for gateway.upgrade."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.upgrade import load_install_meta, run_migrations

# ---------------------------------------------------------------------------
# load_install_meta
# ---------------------------------------------------------------------------

class TestLoadInstallMeta:
    def test_load_install_meta_missing(self, tmp_path: Path):
        """Returns {} when file does not exist."""
        meta_file = tmp_path / "nonexistent.json"
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_git(self, tmp_path: Path):
        """Reads git method correctly."""
        meta_file = tmp_path / "install_meta.json"
        expected = {
            "method": "git",
            "repo_path": "/home/user/agent-chat-gateway",
            "version": "0.1.0",
            "installed_at": "2026-03-27",
        }
        meta_file.write_text(json.dumps(expected))

        result = load_install_meta(meta_file)

        assert result["method"] == "git"
        assert result["repo_path"] == "/home/user/agent-chat-gateway"
        assert result["version"] == "0.1.0"
        assert result["installed_at"] == "2026-03-27"

    def test_load_install_meta_malformed(self, tmp_path: Path):
        """Returns {} on JSON decode error."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("{ not valid json }")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_empty(self, tmp_path: Path):
        """Returns {} for an empty file."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_brew(self, tmp_path: Path):
        """Reads brew method correctly."""
        meta_file = tmp_path / "install_meta.json"
        data = {"method": "brew", "repo_path": None, "version": "0.2.0", "installed_at": "2026-01-01"}
        meta_file.write_text(json.dumps(data))

        result = load_install_meta(meta_file)

        assert result["method"] == "brew"
        assert result["repo_path"] is None


# ---------------------------------------------------------------------------
# run_migrations
# ---------------------------------------------------------------------------

class TestRunMigrations:
    def test_run_migrations_noop(self):
        """run_migrations is a no-op and does not raise."""
        # Should not raise for any version string
        run_migrations("0.1.0")
        run_migrations("0.0.0")
        run_migrations("unknown")
        run_migrations("")

    def test_run_migrations_returns_none(self):
        """run_migrations returns None."""
        result = run_migrations("0.1.0")
        assert result is None


# ---------------------------------------------------------------------------
# run_upgrade
# ---------------------------------------------------------------------------

class TestRunUpgrade:
    """Tests for run_upgrade — all file I/O and subprocess calls are mocked."""

    def test_run_upgrade_unknown_method(self, tmp_path: Path):
        """Exits with error for an unknown install method."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "snap", "version": "0.1.0"}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_brew(self, tmp_path: Path):
        """Calls brew upgrade for brew install method."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "brew", "repo_path": None, "version": "0.1.0"}))

        brew_result = MagicMock()
        brew_result.returncode = 0

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("subprocess.run", return_value=brew_result) as mock_run,
        ):
            run_upgrade()

        mock_run.assert_called_once_with(
            ["brew", "upgrade", "agent-chat-gateway"],
            check=False,
        )

    def test_run_upgrade_brew_failure(self, tmp_path: Path):
        """Exits with error when brew upgrade fails."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "brew", "version": "0.1.0"}))

        brew_result = MagicMock()
        brew_result.returncode = 1

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("subprocess.run", return_value=brew_result),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_git_missing_repo(self, tmp_path: Path):
        """Exits with error when repo_path in meta does not exist."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        missing_repo = tmp_path / "nonexistent-repo"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(missing_repo),
            "version": "0.1.0",
        }))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_git_no_repo_path(self, tmp_path: Path):
        """Exits with error when repo_path is missing from meta."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "git", "version": "0.1.0"}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_git_success(self, tmp_path: Path):
        """Happy path: git pull + uv sync called, meta version updated."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        # Write a fake pyproject.toml so _read_current_version works
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
            "installed_at": "2026-01-01",
        }))

        ok_result = MagicMock()
        ok_result.returncode = 0

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=ok_result) as mock_run,
        ):
            run_upgrade()

        # git pull and uv sync should have been called
        calls = mock_run.call_args_list
        commands = [c.args[0] for c in calls]
        assert ["git", "-C", str(repo), "pull"] in commands
        assert ["uv", "sync"] in commands

        # Meta file should be updated with new version
        updated = json.loads(meta_file.read_text())
        assert updated["version"] == "0.2.0"

    def test_run_upgrade_git_success_daemon_running(self, tmp_path: Path):
        """When daemon is running, it is stopped then restarted."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
            "installed_at": "2026-01-01",
        }))

        ok_result = MagicMock()
        ok_result.returncode = 0

        stop_mock = MagicMock()
        start_mock = MagicMock()

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(True, 12345)),
            patch("gateway.upgrade.stop_daemon", stop_mock),
            patch("gateway.upgrade.start_daemon", start_mock),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=ok_result),
        ):
            run_upgrade()

        stop_mock.assert_called_once()
        start_mock.assert_called_once()

    def test_run_upgrade_git_pull_failure(self, tmp_path: Path):
        """Exits with error when git pull fails."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
        }))

        fail_result = MagicMock()
        fail_result.returncode = 1

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=fail_result),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_missing_meta_not_pip(self, tmp_path: Path):
        """Exits with error when install_meta.json does not exist and not a pip install."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "nonexistent_meta.json"

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade._is_pip_installed", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_pip_no_meta(self, tmp_path: Path):
        """When install_meta.json is missing but pip-installed, runs pip upgrade."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "nonexistent_meta.json"
        ok_result = MagicMock()
        ok_result.returncode = 0

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade._is_pip_installed", return_value=True),
            patch("subprocess.run", return_value=ok_result) as mock_run,
        ):
            run_upgrade()

        called_cmd = mock_run.call_args.args[0]
        assert called_cmd[-2:] == ["--upgrade", "agent-chat-gateway"]

    def test_run_upgrade_pip_failure(self, tmp_path: Path):
        """Exits with error when pip upgrade fails."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "nonexistent_meta.json"
        fail_result = MagicMock()
        fail_result.returncode = 1

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade._is_pip_installed", return_value=True),
            patch("subprocess.run", return_value=fail_result),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1


class TestIsPipInstalled:
    """Tests for _is_pip_installed detection logic."""

    def test_returns_false_when_package_not_found(self):
        """Returns False when importlib.metadata raises PackageNotFoundError."""
        import importlib.metadata

        from gateway.upgrade import _is_pip_installed

        with patch.object(importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError):
            assert _is_pip_installed() is False

    def test_returns_true_when_no_direct_url(self):
        """Returns True when package found and no direct_url.json (regular PyPI install)."""
        import importlib.metadata

        from gateway.upgrade import _is_pip_installed

        mock_dist = MagicMock()
        mock_dist.files = []  # no direct_url.json

        with (
            patch.object(importlib.metadata, "version", return_value="0.1.0"),
            patch.object(importlib.metadata, "distribution", return_value=mock_dist),
        ):
            assert _is_pip_installed() is True

    def test_returns_false_for_editable_install(self):
        """Returns False when direct_url.json indicates editable install."""
        import importlib.metadata

        from gateway.upgrade import _is_pip_installed

        mock_file = MagicMock()
        mock_file.name = "direct_url.json"
        mock_file.read_text.return_value = '{"url": "file:///home/user/repo", "dir_info": {"editable": true}}'

        mock_dist = MagicMock()
        mock_dist.files = [mock_file]

        with (
            patch.object(importlib.metadata, "version", return_value="0.1.0"),
            patch.object(importlib.metadata, "distribution", return_value=mock_dist),
        ):
            assert _is_pip_installed() is False

    def test_returns_false_for_local_directory_install(self):
        """Returns False when direct_url.json indicates local directory install."""
        import importlib.metadata

        from gateway.upgrade import _is_pip_installed

        mock_file = MagicMock()
        mock_file.name = "direct_url.json"
        mock_file.read_text.return_value = '{"url": "file:///home/user/repo", "dir_info": {"editable": false}}'

        mock_dist = MagicMock()
        mock_dist.files = [mock_file]

        with (
            patch.object(importlib.metadata, "version", return_value="0.1.0"),
            patch.object(importlib.metadata, "distribution", return_value=mock_dist),
        ):
            assert _is_pip_installed() is False
