"""Unit tests for gateway/configtool/env_writer.py — upsert_env_vars().

Pins the merge-by-key behavior specifically: onboard.py's existing
_write_env() clobbers the whole file, which is wrong once more than one
connector needs its own secret upserted independently.
"""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from gateway.configtool.env_writer import upsert_env_vars


class TestUpsertEnvVars(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"

    def _permissions(self, path: Path) -> str:
        return oct(stat.S_IMODE(path.stat().st_mode))

    def test_creates_a_new_file_with_the_given_keys(self):
        upsert_env_vars(self.env_path, {"RC_PASSWORD": "hunter2"})
        self.assertEqual(self.env_path.read_text(), "RC_PASSWORD=hunter2\n")

    def test_creates_the_parent_directory_if_missing(self):
        nested = self.tmp / "nested" / "dir" / ".env"
        upsert_env_vars(nested, {"KEY": "value"})
        self.assertTrue(nested.exists())

    def test_restricts_permissions_to_0600(self):
        upsert_env_vars(self.env_path, {"KEY": "value"})
        self.assertEqual(self._permissions(self.env_path), "0o600")

    def test_quotes_values_containing_spaces(self):
        upsert_env_vars(self.env_path, {"NAME": "hello world"})
        self.assertEqual(self.env_path.read_text(), 'NAME="hello world"\n')

    def test_does_not_quote_values_without_spaces(self):
        upsert_env_vars(self.env_path, {"NAME": "no-spaces-here"})
        self.assertEqual(self.env_path.read_text(), "NAME=no-spaces-here\n")

    def test_replaces_an_existing_keys_value_in_place(self):
        self.env_path.write_text("RC_URL=http://old\nRC_PASSWORD=oldpw\n")
        upsert_env_vars(self.env_path, {"RC_PASSWORD": "newpw"})
        self.assertEqual(
            self.env_path.read_text(), "RC_URL=http://old\nRC_PASSWORD=newpw\n"
        )

    def test_appends_a_new_key_without_touching_existing_ones(self):
        self.env_path.write_text("RC_URL=http://old\n")
        upsert_env_vars(self.env_path, {"MM_TOKEN": "abc123"})
        self.assertEqual(
            self.env_path.read_text(), "RC_URL=http://old\nMM_TOKEN=abc123\n"
        )

    def test_preserves_comments_and_blank_lines_and_their_order(self):
        self.env_path.write_text(
            "# top comment\n\nRC_URL=http://old\n# another comment\nRC_PASSWORD=oldpw\n"
        )
        upsert_env_vars(self.env_path, {"RC_PASSWORD": "newpw"})
        self.assertEqual(
            self.env_path.read_text(),
            "# top comment\n\nRC_URL=http://old\n# another comment\nRC_PASSWORD=newpw\n",
        )

    def test_a_commented_out_key_line_is_not_treated_as_the_real_key(self):
        """'# RC_PASSWORD=disabled' must not be matched and replaced — it's
        a comment, not a live KEY=VALUE line. The real (missing) key is
        appended instead."""
        self.env_path.write_text("# RC_PASSWORD=disabled\n")
        upsert_env_vars(self.env_path, {"RC_PASSWORD": "newpw"})
        self.assertEqual(
            self.env_path.read_text(), "# RC_PASSWORD=disabled\nRC_PASSWORD=newpw\n"
        )

    def test_multiple_keys_mixing_replace_and_append(self):
        self.env_path.write_text("RC_URL=http://old\nRC_USERNAME=bot\n")
        upsert_env_vars(
            self.env_path,
            {"RC_USERNAME": "newbot", "MM_TOKEN": "xyz"},
        )
        self.assertEqual(
            self.env_path.read_text(),
            "RC_URL=http://old\nRC_USERNAME=newbot\nMM_TOKEN=xyz\n",
        )

    def test_permissions_restricted_even_when_merging_into_an_existing_file(self):
        self.env_path.write_text("RC_URL=http://old\n")
        self.env_path.chmod(0o644)
        upsert_env_vars(self.env_path, {"RC_URL": "http://new"})
        self.assertEqual(self._permissions(self.env_path), "0o600")


if __name__ == "__main__":
    unittest.main()
