"""Unit tests for gateway.admin.config: AdminProfile validation and
load_profiles/get_profile file handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.admin.config import (
    AdminConfigError,
    AdminProfile,
    get_profile,
    load_profiles,
)


class TestAdminProfileValidation(unittest.TestCase):
    def test_valid_mattermost_profile_with_token(self):
        profile = AdminProfile(
            name="mm", type="mattermost", server_url="https://x", team="t", token="tok"
        )
        self.assertEqual(profile.team, "t")

    def test_valid_rocketchat_profile_with_username_password(self):
        profile = AdminProfile(
            name="rc", type="rocketchat", server_url="https://x",
            username="admin", password="pw",
        )
        self.assertEqual(profile.username, "admin")

    def test_unknown_type_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="discord", server_url="https://x", token="t")

    def test_missing_server_url_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="rocketchat", server_url="", token="t")

    def test_missing_credentials_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="rocketchat", server_url="https://x")

    def test_mattermost_without_team_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="mm", type="mattermost", server_url="https://x", token="t")

    def test_rocketchat_without_team_is_fine(self):
        # RC has no team concept — team is a mattermost-only requirement.
        profile = AdminProfile(
            name="rc", type="rocketchat", server_url="https://x",
            username="admin", password="pw",
        )
        self.assertIsNone(profile.team)


class TestLoadProfiles(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(AdminConfigError):
            load_profiles("/nonexistent/admin-profiles.yaml")

    def test_missing_profiles_section_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("not_profiles: {}\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_non_mapping_profile_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("profiles:\n  bad: not-a-mapping\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_env_var_path_used_when_no_explicit_path_given(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with patch.dict("os.environ", {"ACG_ADMIN_CONFIG": str(path)}):
                profiles = load_profiles()
            self.assertEqual(set(profiles), {"rc-lab"})

    def test_explicit_path_overrides_env_var(self):
        with tempfile.TemporaryDirectory() as d:
            real_path = Path(d) / "real.yaml"
            real_path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with patch.dict("os.environ", {"ACG_ADMIN_CONFIG": "/nonexistent/other.yaml"}):
                profiles = load_profiles(real_path)
            self.assertEqual(set(profiles), {"rc-lab"})

    def test_loads_multiple_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n"
                "  mm-lab:\n"
                "    type: mattermost\n"
                "    server_url: https://mm\n"
                "    team: t\n"
                "    token: tok\n"
                "  rc-lab:\n"
                "    type: rocketchat\n"
                "    server_url: https://rc\n"
                "    username: admin\n"
                "    password: pw\n"
            )
            profiles = load_profiles(path)
            self.assertEqual(set(profiles), {"mm-lab", "rc-lab"})
            self.assertEqual(profiles["mm-lab"].type, "mattermost")
            self.assertEqual(profiles["rc-lab"].type, "rocketchat")


class TestGetProfile(unittest.TestCase):
    def test_returns_matching_profile(self):
        profiles = {
            "mm": AdminProfile(name="mm", type="mattermost", server_url="https://x", team="t", token="tok")
        }
        self.assertIs(get_profile(profiles, "mm"), profiles["mm"])

    def test_unknown_name_raises_with_available_names(self):
        profiles = {
            "mm": AdminProfile(name="mm", type="mattermost", server_url="https://x", team="t", token="tok")
        }
        with self.assertRaises(AdminConfigError) as ctx:
            get_profile(profiles, "nope")
        self.assertIn("mm", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
