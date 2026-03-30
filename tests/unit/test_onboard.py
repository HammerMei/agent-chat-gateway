"""Unit tests for gateway.onboard."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from gateway.onboard import (
    detect_agent_backends,
    generate_config_yaml,
    install_opencode_plugin,
    load_install_meta,
    write_install_meta,
)


# ---------------------------------------------------------------------------
# detect_agent_backends
# ---------------------------------------------------------------------------

class TestDetectAgentBackends:
    def test_detect_backends_both_found(self):
        """Both claude and opencode return exit code 0."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.2.3"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            backends = detect_agent_backends()

        assert "claude" in backends
        assert "opencode" in backends
        assert mock_run.call_count == 2

    def test_detect_backends_none_found(self):
        """Both claude and opencode return non-zero."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            backends = detect_agent_backends()

        assert backends == {}

    def test_detect_backends_claude_only(self):
        """Only claude succeeds."""
        def _side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "claude":
                m.returncode = 0
                m.stdout = "claude 1.0.0"
                m.stderr = ""
            else:
                m.returncode = 1
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_side_effect):
            backends = detect_agent_backends()

        assert "claude" in backends
        assert "opencode" not in backends
        assert backends["claude"] == "claude 1.0.0"

    def test_detect_backends_opencode_only(self):
        """Only opencode succeeds."""
        def _side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "opencode":
                m.returncode = 0
                m.stdout = "opencode 2.0.0"
                m.stderr = ""
            else:
                m.returncode = 1
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_side_effect):
            backends = detect_agent_backends()

        assert "opencode" in backends
        assert "claude" not in backends
        assert backends["opencode"] == "opencode 2.0.0"

    def test_detect_backends_file_not_found(self):
        """FileNotFoundError is handled gracefully — backend treated as absent."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            backends = detect_agent_backends()

        assert backends == {}

    def test_detect_backends_timeout(self):
        """TimeoutExpired is handled gracefully."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
        ):
            backends = detect_agent_backends()

        assert backends == {}

    def test_detect_backends_version_from_stderr(self):
        """If stdout is empty, version falls back to stderr."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = "claude 0.9"

        with patch("subprocess.run", return_value=mock_result):
            backends = detect_agent_backends()

        assert backends["claude"] == "claude 0.9"


# ---------------------------------------------------------------------------
# generate_config_yaml
# ---------------------------------------------------------------------------

class TestGenerateConfigYaml:
    def _parse(self, yaml_str: str) -> dict:
        """Parse YAML and assert it's valid."""
        return yaml.safe_load(yaml_str)

    def test_generate_config_yaml_claude_rocketchat(self):
        """Output is valid YAML with correct top-level keys for claude/rocketchat."""
        connector_data = {
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "secret",
            "owners": ["alice"],
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]

        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        config = self._parse(result)

        assert "connectors" in config
        assert "agents" in config
        assert "watchers" in config

        connector = config["connectors"][0]
        assert connector["type"] == "rocketchat"
        assert connector["name"] == "rc-home"
        assert connector["allowed_users"]["owners"] == ["alice"]
        assert connector["allowed_users"]["guests"] == []

        agent = config["agents"]["my-agent"]
        assert agent["type"] == "claude"
        assert agent["command"] == "claude"
        assert agent["timeout"] == 360
        assert agent["permissions"]["enabled"] is True

        watcher = config["watchers"][0]
        assert watcher["name"] == "dm-alice"
        assert watcher["room"] == "@alice"
        assert watcher["connector"] == "rc-home"
        assert watcher["agent"] == "my-agent"
        assert watcher["session_id"] is None

    def test_generate_config_yaml_opencode_rocketchat(self):
        """Agent type is correctly set to opencode."""
        connector_data = {
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "secret",
            "owners": ["bob"],
        }
        watchers = [{"name": "dm-bob", "room": "@bob"}]

        result = generate_config_yaml("opencode", "rocketchat", connector_data, watchers)
        config = self._parse(result)

        agent = config["agents"]["my-agent"]
        assert agent["type"] == "opencode"
        assert agent["command"] == "opencode"

    def test_generate_config_yaml_multiple_watchers(self):
        """Multiple watchers are all present in the output."""
        connector_data = {
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "secret",
            "owners": ["alice", "charlie"],
        }
        watchers = [
            {"name": "dm-alice", "room": "@alice"},
            {"name": "general", "room": "general"},
            {"name": "dev-room", "room": "dev"},
        ]

        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        config = self._parse(result)

        assert len(config["watchers"]) == 3
        names = [w["name"] for w in config["watchers"]]
        assert "dm-alice" in names
        assert "general" in names
        assert "dev-room" in names

    def test_generate_config_yaml_multiple_owners(self):
        """Multiple owners are listed in allowed_users.owners."""
        connector_data = {
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "secret",
            "owners": ["alice", "bob", "charlie"],
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]

        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        config = self._parse(result)

        owners = config["connectors"][0]["allowed_users"]["owners"]
        assert owners == ["alice", "bob", "charlie"]

    def test_generate_config_yaml_valid_yaml(self):
        """Output is parseable YAML and round-trips cleanly."""
        connector_data = {
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "hunter2",
            "owners": ["alice"],
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]

        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        # Must not raise
        parsed = yaml.safe_load(result)
        # Round-trip: dump and parse again
        reparsed = yaml.safe_load(yaml.dump(parsed))
        assert reparsed["agents"]["my-agent"]["timeout"] == 360

    def test_generate_config_yaml_session_id_is_null(self):
        """session_id renders as null in YAML."""
        connector_data = {"owners": ["alice"], "server_url": "https://x.com", "bot_username": "b", "bot_password": "p"}
        watchers = [{"name": "dm-alice", "room": "@alice"}]
        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        config = self._parse(result)
        assert config["watchers"][0]["session_id"] is None

    def test_generate_config_yaml_notifications_present(self):
        """online_notification and offline_notification are set on each watcher."""
        connector_data = {"owners": ["alice"], "server_url": "https://x.com", "bot_username": "b", "bot_password": "p"}
        watchers = [{"name": "dm-alice", "room": "@alice"}]
        result = generate_config_yaml("claude", "rocketchat", connector_data, watchers)
        config = self._parse(result)
        w = config["watchers"][0]
        assert "online_notification" in w
        assert "offline_notification" in w


# ---------------------------------------------------------------------------
# generate_config_yaml — opencode working_directory
# ---------------------------------------------------------------------------

class TestGenerateConfigYamlOpencode:
    def _parse(self, yaml_str: str) -> dict:
        return yaml.safe_load(yaml_str)

    def test_working_directory_included_for_opencode(self, tmp_path: Path):
        """working_directory is set in agent block when agent_type is opencode."""
        connector_data = {
            "owners": ["alice"],
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "pw",
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]
        result = generate_config_yaml(
            "opencode", "rocketchat", connector_data, watchers,
            working_directory=str(tmp_path),
        )
        config = self._parse(result)
        assert config["agents"]["my-agent"]["working_directory"] == str(tmp_path)

    def test_working_directory_omitted_for_claude(self, tmp_path: Path):
        """working_directory is NOT included when agent_type is claude."""
        connector_data = {
            "owners": ["alice"],
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "pw",
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]
        result = generate_config_yaml(
            "claude", "rocketchat", connector_data, watchers,
            working_directory=str(tmp_path),
        )
        config = self._parse(result)
        assert "working_directory" not in config["agents"]["my-agent"]

    def test_working_directory_omitted_when_none(self):
        """working_directory absent from agent block when not provided."""
        connector_data = {
            "owners": ["alice"],
            "server_url": "https://chat.example.com",
            "bot_username": "bot",
            "bot_password": "pw",
        }
        watchers = [{"name": "dm-alice", "room": "@alice"}]
        result = generate_config_yaml(
            "opencode", "rocketchat", connector_data, watchers,
            working_directory=None,
        )
        config = self._parse(result)
        assert "working_directory" not in config["agents"]["my-agent"]


# ---------------------------------------------------------------------------
# install_opencode_plugin
# ---------------------------------------------------------------------------

class TestInstallOpencodePlugin:
    """Tests for install_opencode_plugin — installs to global ~/.opencode/."""

    def _make_fake_plugin(self, tmp_path: Path) -> Path:
        """Create a fake role-enforcement.ts to act as plugin source."""
        src = tmp_path / "role-enforcement.ts"
        src.write_text("// fake plugin\nexport default function() {}")
        return src

    def _global_dir(self, tmp_path: Path) -> Path:
        """Return a tmp directory to use as the fake global opencode dir."""
        d = tmp_path / "global-opencode"
        d.mkdir()
        return d

    def test_installs_plugin_to_global_dir(self, tmp_path: Path):
        """Plugin file is copied to global_opencode_dir/plugins/."""
        global_dir = self._global_dir(tmp_path)
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)

        assert dest == global_dir / "plugins" / "role-enforcement.ts"
        assert dest.exists()
        assert dest.read_text() == plugin_src.read_text()

    def test_returns_absolute_dest_path(self, tmp_path: Path):
        """install_opencode_plugin returns the absolute path of the installed file."""
        global_dir = self._global_dir(tmp_path)
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)

        assert dest.is_absolute()

    def test_creates_opencode_json_if_missing(self, tmp_path: Path):
        """Creates global opencode.json with absolute plugin path when it does not exist."""
        global_dir = self._global_dir(tmp_path)
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)

        config_file = global_dir / "opencode.json"
        assert config_file.exists()
        config = json.loads(config_file.read_text())
        assert str(dest) in config["plugin"]

    def test_plugin_entry_is_absolute_path(self, tmp_path: Path):
        """Plugin entry in opencode.json is an absolute path, not relative."""
        global_dir = self._global_dir(tmp_path)
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            install_opencode_plugin(global_opencode_dir=global_dir)

        config = json.loads((global_dir / "opencode.json").read_text())
        for entry in config["plugin"]:
            if "role-enforcement" in entry:
                assert Path(entry).is_absolute(), "Plugin entry must be an absolute path"

    def test_patches_existing_opencode_json(self, tmp_path: Path):
        """Adds plugin entry to an existing opencode.json without clobbering other keys."""
        global_dir = self._global_dir(tmp_path)
        existing = {"default_agent": "build", "plugin": ["some-other-plugin.ts"]}
        (global_dir / "opencode.json").write_text(json.dumps(existing))

        plugin_src = self._make_fake_plugin(tmp_path)
        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)

        config = json.loads((global_dir / "opencode.json").read_text())
        assert config["default_agent"] == "build"        # existing key preserved
        assert "some-other-plugin.ts" in config["plugin"]
        assert str(dest) in config["plugin"]

    def test_idempotent_plugin_registration(self, tmp_path: Path):
        """Running install twice does not duplicate the plugin entry."""
        global_dir = self._global_dir(tmp_path)
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)
            install_opencode_plugin(global_opencode_dir=global_dir)

        config = json.loads((global_dir / "opencode.json").read_text())
        assert config["plugin"].count(str(dest)) == 1

    def test_raises_if_plugin_source_missing(self, tmp_path: Path):
        """FileNotFoundError raised when neither module-relative nor repo_path source exists."""
        global_dir = self._global_dir(tmp_path)
        nonexistent = tmp_path / "no-such-file.ts"

        with patch("gateway.onboard._PLUGIN_SRC", nonexistent):
            with pytest.raises(FileNotFoundError, match="opencode plugin source not found"):
                install_opencode_plugin(repo_path=None, global_opencode_dir=global_dir)

    def test_repo_path_fallback(self, tmp_path: Path):
        """Falls back to repo_path when module-relative source is missing."""
        global_dir = self._global_dir(tmp_path)

        repo = tmp_path / "acg-repo"
        hook_dir = repo / "gateway" / "agents" / "opencode" / "hooks"
        hook_dir.mkdir(parents=True)
        (hook_dir / "role-enforcement.ts").write_text("// from repo fallback")

        nonexistent = tmp_path / "no-such-file.ts"
        with patch("gateway.onboard._PLUGIN_SRC", nonexistent):
            dest = install_opencode_plugin(repo_path=repo, global_opencode_dir=global_dir)

        assert dest.read_text() == "// from repo fallback"

    def test_handles_malformed_existing_opencode_json(self, tmp_path: Path):
        """Treats malformed opencode.json as empty dict and overwrites cleanly."""
        global_dir = self._global_dir(tmp_path)
        (global_dir / "opencode.json").write_text("this is { not json")

        plugin_src = self._make_fake_plugin(tmp_path)
        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)  # must not raise

        config = json.loads((global_dir / "opencode.json").read_text())
        assert str(dest) in config["plugin"]

    def test_creates_parent_dirs(self, tmp_path: Path):
        """Creates plugins/ subdir if it does not exist."""
        global_dir = tmp_path / "new-opencode-dir"  # does not exist yet
        plugin_src = self._make_fake_plugin(tmp_path)

        with patch("gateway.onboard._PLUGIN_SRC", plugin_src):
            dest = install_opencode_plugin(global_opencode_dir=global_dir)

        assert dest.exists()


# ---------------------------------------------------------------------------
# write_install_meta / load_install_meta
# ---------------------------------------------------------------------------

class TestInstallMeta:
    def test_write_install_meta_git(self, tmp_path: Path):
        """write_install_meta writes correct JSON for git method."""
        meta_file = tmp_path / "install_meta.json"
        repo = tmp_path / "repo"

        write_install_meta(meta_file, method="git", repo_path=repo, version="0.1.0")

        data = json.loads(meta_file.read_text())
        assert data["method"] == "git"
        assert data["repo_path"] == str(repo)
        assert data["version"] == "0.1.0"
        assert "installed_at" in data
        # installed_at should be a valid date string YYYY-MM-DD
        from datetime import date
        date.fromisoformat(data["installed_at"])  # raises if invalid

    def test_write_install_meta_brew(self, tmp_path: Path):
        """write_install_meta writes correct JSON for brew method."""
        meta_file = tmp_path / "install_meta.json"

        write_install_meta(meta_file, method="brew", repo_path=None, version="0.2.0")

        data = json.loads(meta_file.read_text())
        assert data["method"] == "brew"
        assert data["repo_path"] is None
        assert data["version"] == "0.2.0"

    def test_write_install_meta_creates_parent_dirs(self, tmp_path: Path):
        """write_install_meta creates intermediate directories."""
        meta_file = tmp_path / "nested" / "deep" / "install_meta.json"
        write_install_meta(meta_file, method="git", repo_path=None, version="0.1.0")
        assert meta_file.exists()

    def test_load_install_meta_missing(self, tmp_path: Path):
        """load_install_meta returns {} when file does not exist."""
        meta_file = tmp_path / "nonexistent.json"
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_valid(self, tmp_path: Path):
        """load_install_meta reads valid JSON correctly."""
        meta_file = tmp_path / "install_meta.json"
        expected = {
            "method": "git",
            "repo_path": "/home/user/repo",
            "version": "0.1.0",
            "installed_at": "2026-03-27",
        }
        meta_file.write_text(json.dumps(expected))

        result = load_install_meta(meta_file)
        assert result == expected

    def test_load_install_meta_malformed(self, tmp_path: Path):
        """load_install_meta returns {} on malformed JSON."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("this is not { valid json }")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_empty_file(self, tmp_path: Path):
        """load_install_meta returns {} for an empty file."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_write_then_load_roundtrip(self, tmp_path: Path):
        """write then load produces the same data."""
        meta_file = tmp_path / "install_meta.json"
        repo = tmp_path / "my-repo"

        write_install_meta(meta_file, method="git", repo_path=repo, version="1.2.3")
        loaded = load_install_meta(meta_file)

        assert loaded["method"] == "git"
        assert loaded["repo_path"] == str(repo)
        assert loaded["version"] == "1.2.3"


# ---------------------------------------------------------------------------
# run_onboard (UI-heavy — fully mocked)
# ---------------------------------------------------------------------------

class TestRunOnboard:
    """Test run_onboard with all Rich UI and file I/O mocked."""

    def _make_subprocess_mock(self):
        """Return a subprocess.run mock where claude succeeds, opencode fails."""
        def _side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "claude":
                m.returncode = 0
                m.stdout = "claude 1.0.0"
                m.stderr = ""
            else:
                m.returncode = 1
                m.stdout = ""
                m.stderr = ""
            return m
        return _side_effect

    def test_run_onboard_happy_path(self, tmp_path: Path):
        """run_onboard (claude) writes config, .env, and install_meta.json."""
        from gateway.onboard import run_onboard

        config_file = tmp_path / "config.yaml"
        env_file = tmp_path / ".env"
        meta_file = tmp_path / "install_meta.json"

        prompts = iter([
            "https://chat.example.com",   # server URL
            "bot",                         # bot username
            "secret",                      # bot password
            "alice",                       # owner
        ])
        confirms = iter([
            True,   # use claude?
            False,  # add more rooms?
            True,   # write files?
        ])

        with (
            patch("subprocess.run", side_effect=self._make_subprocess_mock()),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompts)),
            patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirms)),
            patch("gateway.onboard.CONFIG_FILE", config_file),
            patch("gateway.onboard.ENV_FILE", env_file),
            patch("gateway.onboard.META_FILE", meta_file),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
        ):
            run_onboard(repo_path=tmp_path)

        assert config_file.exists()
        assert env_file.exists()
        assert meta_file.exists()

        # Config is valid YAML
        config = yaml.safe_load(config_file.read_text())
        assert "connectors" in config
        assert "agents" in config
        assert "watchers" in config

        # .env has the right keys
        env_text = env_file.read_text()
        assert "RC_URL=https://chat.example.com" in env_text
        assert "RC_USERNAME=bot" in env_text
        assert "RC_PASSWORD=secret" in env_text

        # install_meta has method set
        meta = json.loads(meta_file.read_text())
        assert meta["method"] in ("git", "unknown")

    def test_run_onboard_abort_at_confirm(self, tmp_path: Path):
        """run_onboard exits cleanly when user declines to write files."""
        from gateway.onboard import run_onboard

        config_file = tmp_path / "config.yaml"

        prompts = iter([
            "https://chat.example.com",
            "bot",
            "secret",
            "alice",
        ])
        confirms = iter([
            True,   # use claude
            False,  # add more rooms
            False,  # do NOT write files
        ])

        with (
            patch("subprocess.run", side_effect=self._make_subprocess_mock()),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompts)),
            patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirms)),
            patch("gateway.onboard.CONFIG_FILE", config_file),
            patch("gateway.onboard.ENV_FILE", tmp_path / ".env"),
            patch("gateway.onboard.META_FILE", tmp_path / "install_meta.json"),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_onboard(repo_path=None)

        assert exc_info.value.code == 0
        assert not config_file.exists()

    def test_run_onboard_no_backends_exits(self, tmp_path: Path):
        """run_onboard exits with code 1 when no backends are found."""
        from gateway.onboard import run_onboard

        fail_mock = MagicMock()
        fail_mock.returncode = 1
        fail_mock.stdout = ""
        fail_mock.stderr = ""

        with (
            patch("subprocess.run", return_value=fail_mock),
            patch("gateway.onboard.CONFIG_FILE", tmp_path / "config.yaml"),
            patch("gateway.onboard.ENV_FILE", tmp_path / ".env"),
            patch("gateway.onboard.META_FILE", tmp_path / "install_meta.json"),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_onboard(repo_path=None)

        assert exc_info.value.code == 1

    def test_run_onboard_existing_config_cancel(self, tmp_path: Path):
        """run_onboard exits when user chooses Cancel on existing config."""
        from gateway.onboard import run_onboard

        config_file = tmp_path / "config.yaml"
        config_file.write_text("# existing config")

        with (
            patch("gateway.onboard.CONFIG_FILE", config_file),
            patch("gateway.onboard.ENV_FILE", tmp_path / ".env"),
            patch("gateway.onboard.META_FILE", tmp_path / "install_meta.json"),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
            patch("rich.prompt.Prompt.ask", return_value="3"),  # Cancel
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_onboard(repo_path=None)

        assert exc_info.value.code == 0
        # Original config untouched
        assert config_file.read_text() == "# existing config"

    def test_run_onboard_existing_config_backup(self, tmp_path: Path):
        """run_onboard creates .bak files when user chooses 'start fresh'."""
        from gateway.onboard import run_onboard

        config_file = tmp_path / "config.yaml"
        env_file = tmp_path / ".env"
        config_file.write_text("old: config")
        env_file.write_text("RC_URL=old")

        prompts_iter = iter([
            "2",                            # start fresh (backup)
            "https://chat.example.com",    # server URL
            "bot",                          # bot username
            "secret",                       # bot password
            "alice",                        # owners
        ])
        confirms = iter([
            True,   # use claude
            False,  # add more rooms
            True,   # write files
        ])

        def _proc_mock(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0 if cmd[0] == "claude" else 1
            m.stdout = "1.0.0" if cmd[0] == "claude" else ""
            m.stderr = ""
            return m

        with (
            patch("subprocess.run", side_effect=_proc_mock),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompts_iter)),
            patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirms)),
            patch("gateway.onboard.CONFIG_FILE", config_file),
            patch("gateway.onboard.ENV_FILE", env_file),
            patch("gateway.onboard.META_FILE", tmp_path / "install_meta.json"),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
        ):
            run_onboard(repo_path=tmp_path)

        # At least one .bak file should exist for config
        bak_files = list(tmp_path.glob("config.yaml.bak.*"))
        assert len(bak_files) >= 1
        # New config was written
        new_config = yaml.safe_load(config_file.read_text())
        assert "connectors" in new_config

    def test_run_onboard_opencode_installs_plugin(self, tmp_path: Path):
        """run_onboard with opencode installs plugin globally, sets working_directory in config."""
        from gateway.onboard import run_onboard

        config_file = tmp_path / "config.yaml"
        env_file = tmp_path / ".env"
        meta_file = tmp_path / "install_meta.json"
        opencode_project = tmp_path / "my-project"
        opencode_project.mkdir()
        global_opencode_dir = tmp_path / "global-opencode"

        fake_plugin = tmp_path / "role-enforcement.ts"
        fake_plugin.write_text("// fake plugin")

        def _proc_mock(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0 if cmd[0] == "opencode" else 1
            m.stdout = "opencode 1.0.0" if cmd[0] == "opencode" else ""
            m.stderr = ""
            return m

        prompts = iter([
            "https://chat.example.com",   # server URL
            "bot",                         # bot username
            "secret",                      # bot password
            "alice",                       # owners
            str(opencode_project),         # working directory
        ])
        confirms = iter([
            True,   # use opencode?
            False,  # add more rooms?
            True,   # write files?
        ])

        with (
            patch("subprocess.run", side_effect=_proc_mock),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompts)),
            patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirms)),
            patch("gateway.onboard.CONFIG_FILE", config_file),
            patch("gateway.onboard.ENV_FILE", env_file),
            patch("gateway.onboard.META_FILE", meta_file),
            patch("gateway.onboard.RUNTIME_DIR", tmp_path),
            patch("gateway.onboard._PLUGIN_SRC", fake_plugin),
            patch("gateway.onboard._GLOBAL_OPENCODE_DIR", global_opencode_dir),
        ):
            run_onboard(repo_path=tmp_path)

        # Plugin installed in GLOBAL dir, not in project dir
        global_plugin = global_opencode_dir / "plugins" / "role-enforcement.ts"
        assert global_plugin.exists(), "Plugin should be in global ~/.opencode/plugins/"
        assert not (opencode_project / ".opencode" / "plugins").exists(), \
            "Plugin should NOT be installed in project dir"

        # Global opencode.json uses absolute path
        oc_json = json.loads((global_opencode_dir / "opencode.json").read_text())
        assert str(global_plugin) in oc_json["plugin"]
        assert all(Path(e).is_absolute() for e in oc_json["plugin"] if "role-enforcement" in e)

        # config.yaml has correct working_directory and agent type
        config = yaml.safe_load(config_file.read_text())
        assert config["agents"]["my-agent"]["working_directory"] == str(opencode_project)
        assert config["agents"]["my-agent"]["type"] == "opencode"
