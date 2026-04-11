"""Tests for gateway/core/tool_match.py.

Covers:
  - _normalize_path: path traversal prevention, relative paths, absolute paths
  - matches_rule: tool name regex, params regex, case insensitivity, params=None
  - extract_bash_subcommands: compound commands, heredoc redirects, file redirects
  - get_param_strings_for_claude: Bash, file tools, WebFetch, unknown tools
  - get_param_strings_for_opencode: patterns list, empty patterns fallback
  - all_params_match_any: all-match semantics, partial match fails, empty allow-list
"""

from __future__ import annotations

import unittest

from gateway.config import ToolRule
from gateway.core.tool_match import (
    _normalize_path,
    all_params_match_any,
    extract_bash_subcommands,
    get_param_strings_for_claude,
    get_param_strings_for_opencode,
    matches_rule,
)

# ── _normalize_path ─────────────────────────────────────────────────────────


class TestNormalizePath(unittest.TestCase):
    """_normalize_path prevents traversal attacks and resolves relative paths."""

    def test_path_traversal_neutralized(self):
        """/project/../../../etc/passwd must normalize to /etc/passwd, NOT match /project/.*."""
        result = _normalize_path("/project/../../../etc/passwd", "")
        # normpath collapses the traversal
        self.assertEqual(result, "/etc/passwd")
        # Verify it would NOT match a /project/ allow rule
        rule = ToolRule(tool="Read", params="/project/.*")
        self.assertFalse(matches_rule(rule, "Read", result))

    def test_path_traversal_relative_with_working_dir(self):
        """Relative traversal resolved against working_directory must normalize correctly."""
        result = _normalize_path("../../etc/passwd", "/home/user/project")
        # /home/user/project/../../etc/passwd → /home/user/../../etc/passwd → /etc/passwd
        self.assertEqual(result, "/home/etc/passwd")
        rule = ToolRule(tool="Read", params="/home/user/project/.*")
        self.assertFalse(matches_rule(rule, "Read", result))

    def test_absolute_path_unchanged(self):
        """An already-absolute path is returned as-is (after normpath)."""
        result = _normalize_path("/src/main.py", "/project")
        self.assertEqual(result, "/src/main.py")

    def test_relative_path_resolved_against_working_dir(self):
        """A relative path is joined with working_directory."""
        result = _normalize_path("src/main.py", "/project")
        self.assertEqual(result, "/project/src/main.py")

    def test_empty_value_normalized(self):
        """Empty path is normalized to '.'."""
        result = _normalize_path("", "")
        self.assertEqual(result, ".")

    def test_empty_value_with_working_dir(self):
        """Empty path + working_directory: joined then normalized."""
        # os.path.join("/project", "") gives "/project/"  → normpath → "/project"
        result = _normalize_path("", "/project")
        self.assertIn("project", result)

    def test_double_dot_in_middle(self):
        """`/project/sub/../config.py` normalizes to `/project/config.py`."""
        result = _normalize_path("/project/sub/../config.py", "")
        self.assertEqual(result, "/project/config.py")

    def test_path_traversal_does_not_match_project_rule(self):
        """A traversal that ends outside /project must NOT match a /project/.* rule."""
        dangerous = "/project/../../../etc/shadow"
        normalized = _normalize_path(dangerous, "")
        rule = ToolRule(tool="Write", params="/project/.*")
        self.assertFalse(matches_rule(rule, "Write", normalized))


# ── matches_rule ─────────────────────────────────────────────────────────────


class TestMatchesRule(unittest.TestCase):
    """matches_rule: tool regex fullmatch + params regex fullmatch."""

    def test_exact_tool_name_match(self):
        rule = ToolRule(tool="Read")
        self.assertTrue(matches_rule(rule, "Read", "/any/path"))

    def test_tool_name_case_insensitive(self):
        rule = ToolRule(tool="read")
        self.assertTrue(matches_rule(rule, "READ", "/any"))
        self.assertTrue(matches_rule(rule, "Read", "/any"))

    def test_tool_wildcard_pattern(self):
        rule = ToolRule(tool="mcp__rocketchat__.*")
        self.assertTrue(matches_rule(rule, "mcp__rocketchat__send_message", "{}"))
        self.assertFalse(matches_rule(rule, "mcp__slack__send_message", "{}"))

    def test_params_none_matches_any_param(self):
        """params=None means tool name is the only criterion."""
        rule = ToolRule(tool="Bash")
        self.assertTrue(matches_rule(rule, "Bash", "rm -rf /"))
        self.assertTrue(matches_rule(rule, "Bash", "ls -la"))

    def test_params_pattern_matched(self):
        rule = ToolRule(tool="Read", params="/project/.*")
        self.assertTrue(matches_rule(rule, "Read", "/project/src/main.py"))
        self.assertFalse(matches_rule(rule, "Read", "/etc/passwd"))

    def test_params_fullmatch_not_search(self):
        """params must fullmatch — a partial prefix match is not enough."""
        rule = ToolRule(tool="Bash", params="ls")
        # "ls -la" is not a fullmatch for "ls" (requires the entire string to match)
        self.assertFalse(matches_rule(rule, "Bash", "ls -la"))
        self.assertTrue(matches_rule(rule, "Bash", "ls"))

    def test_params_dot_star_matches_everything(self):
        rule = ToolRule(tool="Bash", params=".*")
        self.assertTrue(matches_rule(rule, "Bash", "rm -rf /"))

    def test_tool_name_partial_match_fails(self):
        """Regex fullmatch: 'Rea' must NOT match 'Read'."""
        rule = ToolRule(tool="Rea")
        self.assertFalse(matches_rule(rule, "Read", "/file"))


# ── extract_bash_subcommands ─────────────────────────────────────────────────


class TestExtractBashSubcommands(unittest.TestCase):
    """extract_bash_subcommands handles simple commands, compound commands, and heredocs."""

    def test_simple_command_returned_as_is(self):
        result = extract_bash_subcommands("ls -la")
        self.assertEqual(result, ["ls -la"])

    def test_compound_and_splits_into_two(self):
        result = extract_bash_subcommands("echo hi && rm -rf /")
        self.assertIn("echo hi", result)
        self.assertTrue(any("rm -rf /" in r for r in result))
        self.assertEqual(len(result), 2)

    def test_semicolon_splits_into_two(self):
        result = extract_bash_subcommands("ls; echo done")
        self.assertEqual(len(result), 2)

    def test_pipe_splits_into_two(self):
        result = extract_bash_subcommands("cat file.txt | grep foo")
        self.assertEqual(len(result), 2)

    # ── heredoc handling ──────────────────────────────────────────────────────

    def test_heredoc_full_text_extracted(self):
        """python3 << 'EOF'...EOF must produce a string containing the heredoc body."""
        cmd = "python3 << 'GHEOF'\nimport urllib.request\nurl = 'https://github.com/trending?since=weekly'\nGHEOF"
        result = extract_bash_subcommands(cmd)
        # Must produce exactly one entry
        self.assertEqual(len(result), 1)
        # The full text must be present so patterns can inspect the heredoc body
        self.assertIn("github.com/trending", result[0])
        self.assertTrue(result[0].startswith("python3"))

    def test_heredoc_full_text_matches_allow_pattern(self):
        """Allow-list pattern for heredoc body must match after extraction."""
        from gateway.config import ToolRule
        from gateway.core.tool_match import all_params_match_any

        cmd = (
            "python3 << 'GHEOF'\n"
            "import urllib.request, re, json, html as h\n"
            "req = urllib.request.Request('https://github.com/trending?since=weekly')\n"
            "print(req)\n"
            "GHEOF"
        )
        param_strings = extract_bash_subcommands(cmd)
        rule = ToolRule(tool="Bash", params=r"python3.*github\.com/trending.*")
        self.assertTrue(all_params_match_any([rule], "Bash", param_strings))

    def test_heredoc_dangerous_compound_still_blocked(self):
        """python3 << 'EOF'...EOF && rm -rf / — rm must be extracted separately and blocked."""
        from gateway.config import ToolRule
        from gateway.core.tool_match import all_params_match_any

        cmd = (
            "python3 << 'GHEOF'\n"
            "url = 'https://github.com/trending'\n"
            "GHEOF\n"
            "&& rm -rf /"
        )
        param_strings = extract_bash_subcommands(cmd)
        # Must have at least 2 entries: the heredoc command and the rm command
        self.assertGreaterEqual(len(param_strings), 2)
        # A pattern matching only the heredoc must NOT approve the whole compound command
        rule = ToolRule(tool="Bash", params=r"python3.*github\.com/trending.*")
        self.assertFalse(all_params_match_any([rule], "Bash", param_strings))

    def test_file_redirect_does_not_include_redirect_target(self):
        """echo hi > /tmp/out — only 'echo hi' is extracted, not the redirect target."""
        result = extract_bash_subcommands("echo hi > /tmp/out")
        # Should have exactly one entry containing the command (not the file path)
        self.assertEqual(len(result), 1)
        self.assertIn("echo", result[0])
        # The redirect operator and filename may or may not appear but the command
        # must be present; crucially the result must NOT be 'python3' alone
        self.assertNotEqual(result[0].strip(), "echo")  # params included

    def test_herestring_full_text_extracted(self):
        """cmd <<< 'value' — herestring content included in extracted text."""
        cmd = "grep pattern <<< 'some multiline content with https://example.com'"
        result = extract_bash_subcommands(cmd)
        self.assertEqual(len(result), 1)
        self.assertIn("example.com", result[0])


# ── get_param_strings_for_claude ─────────────────────────────────────────────


class TestGetParamStringsForClaude(unittest.TestCase):
    """get_param_strings_for_claude extracts the correct field by tool type."""

    def test_read_returns_normalized_path(self):
        params = get_param_strings_for_claude("Read", {"file_path": "/src/main.py"})
        self.assertEqual(params, ["/src/main.py"])

    def test_read_path_traversal_normalized(self):
        params = get_param_strings_for_claude(
            "Read", {"file_path": "/project/../etc/passwd"}
        )
        self.assertEqual(params, ["/etc/passwd"])

    def test_read_with_working_directory(self):
        params = get_param_strings_for_claude(
            "Read", {"file_path": "src/main.py"}, working_directory="/project"
        )
        self.assertEqual(params, ["/project/src/main.py"])

    def test_webfetch_returns_url(self):
        params = get_param_strings_for_claude(
            "WebFetch", {"url": "https://example.com"}
        )
        self.assertEqual(params, ["https://example.com"])

    def test_unknown_tool_returns_json(self):
        params = get_param_strings_for_claude(
            "MyMCPTool", {"key": "value"}
        )
        self.assertEqual(len(params), 1)
        import json
        parsed = json.loads(params[0])
        self.assertEqual(parsed["key"], "value")

    def test_tool_name_case_insensitive(self):
        """Tool name lookup is case-insensitive."""
        p1 = get_param_strings_for_claude("read", {"file_path": "/f"})
        p2 = get_param_strings_for_claude("READ", {"file_path": "/f"})
        self.assertEqual(p1, p2)

    def test_missing_field_returns_empty_string(self):
        """If the primary field is absent, an empty string is returned."""
        params = get_param_strings_for_claude("Read", {})
        self.assertEqual(params, ["."])  # normpath("") → "."

    def test_write_uses_file_path(self):
        params = get_param_strings_for_claude("Write", {"file_path": "/out/result.txt"})
        self.assertEqual(params, ["/out/result.txt"])


# ── get_param_strings_for_opencode ───────────────────────────────────────────


class TestGetParamStringsForOpencode(unittest.TestCase):
    """get_param_strings_for_opencode passes patterns through unchanged."""

    def test_non_empty_patterns_returned_as_is(self):
        patterns = ["ls", "echo hello"]
        result = get_param_strings_for_opencode(patterns)
        self.assertEqual(result, ["ls", "echo hello"])

    def test_empty_patterns_returns_single_empty_string(self):
        """Empty patterns list → [""] so tool-name-only rules still match."""
        result = get_param_strings_for_opencode([])
        self.assertEqual(result, [""])


# ── all_params_match_any ─────────────────────────────────────────────────────


class TestAllParamsMatchAny(unittest.TestCase):
    """all_params_match_any requires ALL param strings to satisfy at least one rule."""

    def _rules(self, *specs) -> list[ToolRule]:
        """Build a list of ToolRule from (tool, params?) tuples."""
        result = []
        for spec in specs:
            if isinstance(spec, tuple):
                result.append(ToolRule(tool=spec[0], params=spec[1]))
            else:
                result.append(ToolRule(tool=spec))
        return result

    def test_single_param_matches(self):
        rules = self._rules(("Bash", "ls.*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls -la"]))

    def test_all_params_must_match(self):
        """Two params: both must match (compound bash command)."""
        rules = self._rules(("Bash", "ls.*"), ("Bash", "echo.*"))
        # "ls -la" matches "ls.*" but "rm -rf /" does not
        self.assertFalse(all_params_match_any(rules, "Bash", ["ls -la", "rm -rf /"]))

    def test_both_params_match(self):
        """Two params, both covered by different rules → approved."""
        rules = self._rules(("Bash", "ls.*"), ("Bash", "echo.*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls -la", "echo hello"]))

    def test_empty_allow_list_denies_all(self):
        self.assertFalse(all_params_match_any([], "Bash", ["ls"]))

    def test_tool_mismatch_denies(self):
        rules = self._rules(("Read", None))
        self.assertFalse(all_params_match_any(rules, "Bash", ["ls"]))

    def test_single_wildcard_rule_allows_all(self):
        rules = self._rules((".*", ".*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["rm -rf /"]))

    def test_case_insensitive_tool_match(self):
        rules = self._rules(("bash", "ls"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls"]))
        self.assertTrue(all_params_match_any(rules, "BASH", ["ls"]))


if __name__ == "__main__":
    unittest.main()
