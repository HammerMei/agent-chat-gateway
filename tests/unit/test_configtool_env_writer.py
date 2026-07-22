"""Unit tests for gateway/configtool/env_writer.py — read_env_vars().

docs/design/config-tool.md decision 6 revisited: the config tool no longer
writes `.env` (upsert_env_vars()/remove_env_vars() were removed along with
their tests once nothing called them anymore) — read_env_vars() remains,
used to resolve an existing $VAR/${VAR} reference for display/editing and
by gateway/config_migrate.py's one-time migration.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.configtool.env_writer import read_env_vars


class TestReadEnvVars(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"

    def test_returns_empty_dict_when_file_does_not_exist(self):
        self.assertEqual(read_env_vars(self.env_path), {})

    def test_reads_simple_key_value_pairs(self):
        self.env_path.write_text("RC_URL=http://old\nRC_PASSWORD=hunter2\n")
        self.assertEqual(
            read_env_vars(self.env_path),
            {"RC_URL": "http://old", "RC_PASSWORD": "hunter2"},
        )

    def test_unquotes_a_quoted_value(self):
        self.env_path.write_text('NAME="hello world"\n')
        self.assertEqual(read_env_vars(self.env_path), {"NAME": "hello world"})

    def test_ignores_comments_and_blank_lines(self):
        self.env_path.write_text("# a comment\n\nRC_URL=http://old\n")
        self.assertEqual(read_env_vars(self.env_path), {"RC_URL": "http://old"})

    def test_a_commented_out_key_is_not_read(self):
        self.env_path.write_text("# RC_PASSWORD=disabled\n")
        self.assertEqual(read_env_vars(self.env_path), {})


if __name__ == "__main__":
    unittest.main()
