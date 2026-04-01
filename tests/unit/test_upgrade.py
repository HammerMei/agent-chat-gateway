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


# ---------------------------------------------------------------------------
# _file_hash
# ---------------------------------------------------------------------------

class TestFileHash:
    """Tests for _file_hash helper."""

    def test_returns_sha256_for_existing_file(self, tmp_path: Path):
        """Returns a hex SHA256 digest for a file that exists."""
        import hashlib

        from gateway.upgrade import _file_hash

        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert _file_hash(f) == expected

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        """Returns None when the file does not exist."""
        from gateway.upgrade import _file_hash

        assert _file_hash(tmp_path / "nonexistent.txt") is None

    def test_different_contents_yield_different_hashes(self, tmp_path: Path):
        """Two files with different contents produce different hashes."""
        from gateway.upgrade import _file_hash

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"content-a")
        b.write_bytes(b"content-b")
        assert _file_hash(a) != _file_hash(b)

    def test_same_contents_yield_same_hash(self, tmp_path: Path):
        """Two files with identical contents produce the same hash."""
        from gateway.upgrade import _file_hash

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"identical")
        b.write_bytes(b"identical")
        assert _file_hash(a) == _file_hash(b)


# ---------------------------------------------------------------------------
# _snapshot_context_hashes
# ---------------------------------------------------------------------------

class TestSnapshotContextHashes:
    """Tests for _snapshot_context_hashes."""

    def test_returns_empty_when_no_contexts_dir(self, tmp_path: Path):
        """Returns {} when repo has no contexts/ directory."""
        from gateway.upgrade import _snapshot_context_hashes

        assert _snapshot_context_hashes(tmp_path) == {}

    def test_returns_hashes_for_all_files(self, tmp_path: Path):
        """Returns a filename→hash dict for every file in contexts/."""
        import hashlib

        from gateway.upgrade import _snapshot_context_hashes

        ctx = tmp_path / "contexts"
        ctx.mkdir()
        (ctx / "a.md").write_bytes(b"content-a")
        (ctx / "b.md").write_bytes(b"content-b")

        result = _snapshot_context_hashes(tmp_path)
        assert set(result.keys()) == {"a.md", "b.md"}
        assert result["a.md"] == hashlib.sha256(b"content-a").hexdigest()
        assert result["b.md"] == hashlib.sha256(b"content-b").hexdigest()

    def test_ignores_subdirectories(self, tmp_path: Path):
        """Subdirectories inside contexts/ are not included."""
        from gateway.upgrade import _snapshot_context_hashes

        ctx = tmp_path / "contexts"
        ctx.mkdir()
        (ctx / "file.md").write_bytes(b"content")
        (ctx / "subdir").mkdir()

        result = _snapshot_context_hashes(tmp_path)
        assert set(result.keys()) == {"file.md"}

    def test_empty_contexts_dir_returns_empty(self, tmp_path: Path):
        """Returns {} when contexts/ directory exists but is empty."""
        from gateway.upgrade import _snapshot_context_hashes

        (tmp_path / "contexts").mkdir()
        assert _snapshot_context_hashes(tmp_path) == {}


# ---------------------------------------------------------------------------
# _sync_context_files
# ---------------------------------------------------------------------------

class TestSyncContextFiles:
    """Tests for the smart context file sync decision table."""

    def _make_repo(self, tmp_path: Path, files: dict[str, bytes]) -> Path:
        """Create a fake repo with contexts/ files and return the repo path."""
        repo = tmp_path / "repo"
        ctx = repo / "contexts"
        ctx.mkdir(parents=True)
        for name, content in files.items():
            (ctx / name).write_bytes(content)
        return repo

    def _make_runtime(self, tmp_path: Path, files: dict[str, bytes]) -> Path:
        """Create a fake runtime dir with contexts/ files and return it."""
        runtime = tmp_path / "runtime"
        ctx = runtime / "contexts"
        ctx.mkdir(parents=True)
        for name, content in files.items():
            (ctx / name).write_bytes(content)
        return runtime

    def test_brand_new_file_is_copied(self, tmp_path: Path):
        """A file in repo but absent from pre_pull_hashes is copied unconditionally."""
        from gateway.upgrade import _sync_context_files

        repo = self._make_repo(tmp_path, {"new.md": b"new content"})
        runtime = self._make_runtime(tmp_path, {})

        _sync_context_files(repo, runtime, pre_pull_hashes={})

        assert (runtime / "contexts" / "new.md").read_bytes() == b"new content"

    def test_missing_user_file_is_copied(self, tmp_path: Path):
        """A file that exists in the repo but not in runtime is copied (first-upgrade case)."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        content = b"existing content"
        repo = self._make_repo(tmp_path, {"existing.md": content})
        # Runtime has no contexts dir at all — simulate old install
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        pre_pull_hashes = {"existing.md": hashlib.sha256(content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        assert (runtime / "contexts" / "existing.md").read_bytes() == content

    def test_unchanged_repo_file_is_skipped(self, tmp_path: Path):
        """When repo file hasn't changed (hash matches pre-pull), user copy is not touched."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        content = b"unchanged content"
        repo = self._make_repo(tmp_path, {"ctx.md": content})
        runtime = self._make_runtime(tmp_path, {"ctx.md": b"user modified version"})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # User's copy should be untouched
        assert (runtime / "contexts" / "ctx.md").read_bytes() == b"user modified version"

    def test_changed_file_unmodified_by_user_is_overwritten(self, tmp_path: Path):
        """Repo file changed + user copy still matches old repo → overwrite with new version."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_content = b"old repo content"
        new_content = b"new repo content"
        repo = self._make_repo(tmp_path, {"ctx.md": new_content})
        # User copy matches the OLD repo version (unmodified)
        runtime = self._make_runtime(tmp_path, {"ctx.md": old_content})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(old_content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        assert (runtime / "contexts" / "ctx.md").read_bytes() == new_content

    def test_changed_file_modified_by_user_saves_default(self, tmp_path: Path):
        """Repo file changed + user copy diverged → save new version as .default, warn."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_content = b"old repo content"
        new_content = b"new repo content"
        user_content = b"user customized content"

        repo = self._make_repo(tmp_path, {"ctx.md": new_content})
        runtime = self._make_runtime(tmp_path, {"ctx.md": user_content})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(old_content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # Original user copy must be untouched
        assert (runtime / "contexts" / "ctx.md").read_bytes() == user_content
        # New repo version saved as .default
        assert (runtime / "contexts" / "ctx.md.default").read_bytes() == new_content

    def test_creates_contexts_dir_if_absent(self, tmp_path: Path):
        """Creates runtime/contexts/ if it does not exist yet."""
        from gateway.upgrade import _sync_context_files

        repo = self._make_repo(tmp_path, {"new.md": b"content"})
        runtime = tmp_path / "runtime"
        runtime.mkdir()  # No contexts/ subdir

        _sync_context_files(repo, runtime, pre_pull_hashes={})

        assert (runtime / "contexts" / "new.md").exists()

    def test_no_contexts_dir_in_repo_is_noop(self, tmp_path: Path):
        """Does nothing when repo has no contexts/ directory."""
        from gateway.upgrade import _sync_context_files

        repo = tmp_path / "repo"
        repo.mkdir()
        runtime = self._make_runtime(tmp_path, {})

        # Should not raise
        _sync_context_files(repo, runtime, pre_pull_hashes={})

    def test_multiple_files_handled_independently(self, tmp_path: Path):
        """Each file follows its own decision path independently."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_a = b"old-a"
        new_a = b"new-a"
        unchanged_b = b"b-content"

        repo = self._make_repo(tmp_path, {"a.md": new_a, "b.md": unchanged_b})
        runtime = self._make_runtime(tmp_path, {"a.md": old_a, "b.md": unchanged_b})

        pre_pull_hashes = {
            "a.md": hashlib.sha256(old_a).hexdigest(),
            "b.md": hashlib.sha256(unchanged_b).hexdigest(),
        }

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # a.md was updated in repo and user had old version → overwrite
        assert (runtime / "contexts" / "a.md").read_bytes() == new_a
        # b.md unchanged in repo → untouched
        assert (runtime / "contexts" / "b.md").read_bytes() == unchanged_b


# ---------------------------------------------------------------------------
# _find_uv
# ---------------------------------------------------------------------------

class TestFindUv:
    """Tests for _find_uv path resolution with fallbacks."""

    def test_returns_shutil_which_result_when_on_path(self):
        """Returns the path from shutil.which when uv is on PATH."""
        from gateway.upgrade import _find_uv

        with patch("shutil.which", return_value="/usr/local/bin/uv"):
            assert _find_uv() == "/usr/local/bin/uv"

    def test_falls_back_to_local_bin(self, tmp_path: Path):
        """Falls back to ~/.local/bin/uv when shutil.which returns None."""
        from gateway.upgrade import _find_uv

        fake_uv = tmp_path / ".local" / "bin" / "uv"
        fake_uv.parent.mkdir(parents=True)
        fake_uv.touch()

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert _find_uv() == str(fake_uv)

    def test_falls_back_to_cargo_bin(self, tmp_path: Path):
        """Falls back to ~/.cargo/bin/uv when ~/.local/bin/uv is absent."""
        from gateway.upgrade import _find_uv

        fake_uv = tmp_path / ".cargo" / "bin" / "uv"
        fake_uv.parent.mkdir(parents=True)
        fake_uv.touch()

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert _find_uv() == str(fake_uv)

    def test_exits_when_uv_not_found_anywhere(self, tmp_path: Path):
        """Calls sys.exit(1) when uv cannot be located by any method."""
        from gateway.upgrade import _find_uv

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            _find_uv()

        assert exc_info.value.code == 1
