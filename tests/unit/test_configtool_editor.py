"""Unit tests for gateway/configtool/editor.py."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gateway.configtool.editor import resolve_editor_command


class TestResolveEditorCommand(unittest.TestCase):
    def test_uses_editor_env_var_when_set(self):
        with patch.dict("os.environ", {"EDITOR": "vim", "VISUAL": "emacs"}, clear=False):
            self.assertEqual(
                resolve_editor_command("/tmp/config.yaml"), ["vim", "/tmp/config.yaml"]
            )

    def test_falls_back_to_visual_when_editor_unset(self):
        with patch.dict("os.environ", {"VISUAL": "emacs"}, clear=False):
            import os
            os.environ.pop("EDITOR", None)
            self.assertEqual(
                resolve_editor_command("/tmp/config.yaml"), ["emacs", "/tmp/config.yaml"]
            )

    def test_falls_back_to_nano_when_neither_set(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("EDITOR", None)
            os.environ.pop("VISUAL", None)
            self.assertEqual(
                resolve_editor_command("/tmp/config.yaml"), ["nano", "/tmp/config.yaml"]
            )

    def test_editor_with_flags_is_split_correctly(self):
        with patch.dict("os.environ", {"EDITOR": "code --wait"}, clear=False):
            self.assertEqual(
                resolve_editor_command("/tmp/config.yaml"),
                ["code", "--wait", "/tmp/config.yaml"],
            )

    def test_editor_with_quoted_path_in_flags_is_split_correctly(self):
        with patch.dict("os.environ", {"EDITOR": "vim -R"}, clear=False):
            self.assertEqual(
                resolve_editor_command("/tmp/config.yaml"),
                ["vim", "-R", "/tmp/config.yaml"],
            )


if __name__ == "__main__":
    unittest.main()
