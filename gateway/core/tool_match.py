"""Tool rule matching utilities shared by all permission brokers.

Each broker extracts a list of "parameter strings" from the tool call payload
(format differs between Claude and OpenCode), then requires ALL of them to
match at least one allow rule before auto-approving.

Primary parameter field mapping (Claude tool_input):
  Bash / bash        → tool_input["command"]  (split into sub-commands via tree-sitter)
  WebFetch / webfetch → tool_input["url"]
  Read / Edit / Write → tool_input["file_path"]  (normalized via os.path.normpath)
  unknown / MCP      → full tool_input serialized as JSON

For OpenCode, patterns[] from the SSE permission event are used directly —
OpenCode already normalizes and splits compound bash commands into one pattern
per AST node.  All patterns must match for auto-approve.

Security notes:
  - Bash: compound commands (e.g. "echo hi && rm -rf /") are split by tree-sitter
    into individual sub-commands; ALL sub-commands must satisfy the params regex.
    Command substitutions ($(...) / backticks) and process substitutions
    (<(...) / >(...)) are recursed into: the nested command is returned as its
    own sub-command and must match a rule too, so "coop fetch-history $(rm -rf x)"
    needs both "coop fetch-history $(rm -rf x)" and "rm -rf x" to be allowed.
  - File paths: os.path.normpath() is applied before matching to prevent
    path-traversal bypasses ("/project/../../../etc/passwd").
  - WebFetch: avoid ".*" as params — it allows fetching internal network addresses
    (localhost, 169.254.169.254 AWS metadata, etc.).  Use explicit domain patterns.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ToolRule

logger = logging.getLogger("coop.permissions.tool_match")

# Maps lowercase tool names to their primary parameter field in Claude's tool_input.
_CLAUDE_PARAM_FIELD: dict[str, str] = {
    "bash": "command",
    "webfetch": "url",
    "read": "file_path",
    "edit": "file_path",
    "write": "file_path",
    "multiedit": "file_path",
    "notebookedit": "notebook_path",
    "skill": "skill",  # Claude Code Skill tool — primary field is the skill name
}

# File tools whose primary field is a path that needs normalization.
_FILE_TOOLS: frozenset[str] = frozenset({
    "read", "edit", "write", "multiedit", "notebookedit",
})

# ── tree-sitter bash parser (optional — falls back gracefully if not installed) ──

_bash_parser = None  # set to a Parser instance on first use


def _get_bash_parser():
    """Return a cached tree-sitter bash Parser, or None if tree-sitter is unavailable."""
    global _bash_parser
    if _bash_parser is not None:
        return _bash_parser

    try:
        import tree_sitter_bash as tsbash  # type: ignore[import]
        from tree_sitter import Language, Parser  # type: ignore[import]

        lang = Language(tsbash.language())
        parser = Parser(lang)
        _bash_parser = parser
        return _bash_parser
    except ImportError:
        logger.warning(
            "tree-sitter or tree-sitter-bash not installed — compound bash command "
            "splitting is disabled.  Install with: pip install tree-sitter tree-sitter-bash"
        )
        return None


# Text that opens a substitution bash will execute.  The AST is not a complete
# oracle for these: with tree-sitter-bash 0.25.1 a ``$(...)`` on an indented
# heredoc line, any backtick in a heredoc body, or a backtick inside a
# ``${x:-...}`` / ``${x#...}`` expansion, comes back as a plain
# ``heredoc_body`` / ``word`` / ``regex`` leaf with no
# ``command_substitution`` child, while bash runs it.  So every named leaf the
# walker reaches is also scanned for these markers, and a hit is returned as its
# own sub-command (fail closed: it must match a rule on its own).
_SUBSTITUTION_MARKERS: tuple[str, ...] = ("$(", "`", "<(", ">(")
_ESCAPED_CHAR = re.compile(r"\\.", re.DOTALL)

# Leaves bash never expands, so a marker inside them is literal text.  A quoted
# heredoc body is the other case and is handled where the heredoc is walked.
_NEVER_EXPANDED_LEAVES: frozenset[str] = frozenset({
    "raw_string",     # '...'
    "ansi_c_string",  # $'...'
    "comment",
})


def _heredoc_is_quoted(heredoc_redirect, src: bytes) -> bool:
    """True when bash will not expand the heredoc body.

    bash treats the body as quoted if *any part* of the delimiter is quoted:
    ``<< 'EOF'``, ``<< "EOF"``, ``<< \\EOF``, but also ``<< E"OF"``, ``<< E\\OF``
    and ``<< $'EOF'``.  So any quote or backslash anywhere in the delimiter
    counts, not only a leading one.
    """
    for child in heredoc_redirect.children:
        if child.type == "heredoc_start":
            start = src[child.start_byte:child.end_byte].decode()
            return any(ch in start for ch in ("'", '"', "\\"))
    return False


def extract_bash_subcommands(command: str) -> list[str]:
    """Split a compound bash command string into individual sub-command strings.

    Uses tree-sitter-bash to parse the AST.  Each ``command`` node (i.e. a
    leaf command in the pipeline/list) is returned as a separate string so the
    caller can require ALL of them to satisfy the allow rule.

    Command substitutions (``$(...)`` / backticks) and process substitutions
    (``<(...)`` / ``>(...)``) are **recursed into**: the parent command is
    returned with the substitution text still in place, and every ``command``
    nested inside the substitution is returned as well, wherever it sits — a
    bare word, a quoted string, a ``${var:-$(...)}`` expansion, a variable
    assignment prefix, a redirect target, a herestring or an unquoted heredoc
    body.  Requiring all of them to match is what stops ``$(rm -rf x)`` riding
    through a ``.*`` rule on the parent.  OpenCode's shell tool does the same
    (``descendantsOfType("command")``), so both brokers see the same list.

    The parser misses some substitutions bash executes (an indented line in an
    unquoted heredoc body; a backtick inside a ``${x:-...}`` expansion — see
    ``_SUBSTITUTION_MARKERS``).  Any named leaf that still contains ``$(``, a
    backtick, ``<(`` or ``>(`` and is not one bash never expands (single-quoted
    string, ``$'...'``, comment, quoted heredoc body) is therefore returned as a
    sub-command of its own, so it fails closed against every rule that does not
    match that raw text.

    Heredoc redirections (``cmd << 'EOF' ... EOF``): the full
    ``redirected_statement`` text is returned as a single string so that
    allow-list patterns can inspect the heredoc body content (e.g. a Python
    script piped to the interpreter).  For regular file redirections
    (``> file``, ``< file``, etc.) only the ``command`` child is returned —
    consistent with non-redirected commands.

    Falls back to ``[command]`` (treat whole string as one command) when:
      - tree-sitter is not installed
      - the parser produces an empty result (shouldn't happen for valid bash)
    """
    parser = _get_bash_parser()
    if parser is None:
        return [command]

    src = command.encode()
    tree = parser.parse(src)
    commands: list[str] = []

    def descend(node) -> None:
        for child in node.children:
            walk(child)

    def walk(node) -> None:
        if node.child_count == 0:
            if node.is_named and node.type not in _NEVER_EXPANDED_LEAVES:
                text = src[node.start_byte:node.end_byte].decode()
                # A backslash-escaped ``\``` or ``\$`` is literal in a word, a
                # double-quoted string and an unquoted heredoc body alike, so
                # drop escaped characters before looking for an opener.
                unescaped = _ESCAPED_CHAR.sub("", text)
                if any(marker in unescaped for marker in _SUBSTITUTION_MARKERS):
                    # A substitution the parser did not turn into a node (see
                    # ``_SUBSTITUTION_MARKERS``).  bash will still run it, so it
                    # becomes a sub-command of its own: only a rule that matches
                    # this raw text approves it.
                    commands.append(text)
            return
        if node.type == "command":
            commands.append(src[node.start_byte:node.end_byte].decode())
            # A substitution nested in this command's words, strings, assignments
            # or herestring is a ``command_substitution`` / ``process_substitution``
            # child; the ``command`` nodes inside it are collected by the descent.
            descend(node)
            return
        if node.type == "redirected_statement":
            # When a command uses a heredoc redirect (e.g. ``python3 << 'EOF'``),
            # the heredoc body is logically the command's stdin input and may
            # contain security-relevant content (e.g. a Python script).
            # Extract the full ``redirected_statement`` text so allow-list patterns
            # can inspect the heredoc body.
            #
            # For non-heredoc redirections (file I/O, e.g. ``echo hi > /tmp/f``),
            # fall through to normal child traversal so only the ``command`` node
            # text is extracted — consistent with prior behaviour.
            has_heredoc = any(
                child.type in ("heredoc_redirect", "herestring_redirect")
                for child in node.children
            )
            if has_heredoc:
                commands.append(src[node.start_byte:node.end_byte].decode())
                # The ``command`` child is already covered by the full text;
                # descend into it (not ``walk`` it, which would append the bare
                # command a second time) and walk the redirects so substitutions
                # in an unquoted heredoc body are collected too.  tree-sitter
                # emits no substitution nodes inside a quoted heredoc
                # (``<< 'EOF'``), matching bash, which does not expand them.
                for child in node.children:
                    if child.type == "command":
                        descend(child)
                    elif child.type == "heredoc_redirect" and _heredoc_is_quoted(child, src):
                        # A quoted delimiter (``'EOF'``, ``E"OF"``, ``\EOF``, …): bash
                        # does not expand the body, so a ``$(`` in it is text.
                        continue
                    else:
                        walk(child)
                return
        descend(node)

    walk(tree.root_node)
    return commands or [command]  # fallback: treat whole string as one command


def _normalize_path(value: str, working_directory: str) -> str:
    """Return a normalized absolute path string for use in regex matching.

    Resolves relative paths against ``working_directory`` and collapses any
    ``..`` components using ``os.path.normpath``.  This prevents path-traversal
    bypasses such as ``/project/../../../etc/passwd``.

    ``os.path.normpath`` (not ``os.path.realpath``) is used intentionally so
    this works for files that do not exist yet (e.g. a ``Write`` creating a new
    file).
    """
    if not os.path.isabs(value) and working_directory:
        value = os.path.join(working_directory, value)
    return os.path.normpath(value)


# ── Public API ─────────────────────────────────────────────────────────────────


def get_param_strings_for_claude(
    tool_name: str,
    tool_input: dict,
    working_directory: str = "",
) -> list[str]:
    """Return the list of parameter strings to match for a Claude PreToolUse event.

    For Bash, returns one string per AST sub-command (requires tree-sitter).
    For file tools, returns the normalized absolute path.
    For all other known tools, returns [primary_field_value].
    For unknown / MCP tools, returns [full_tool_input_as_json].

    All strings in the returned list must satisfy an allow rule for the tool
    call to be auto-approved.
    """
    tool_lower = tool_name.lower()
    field = _CLAUDE_PARAM_FIELD.get(tool_lower)

    if tool_lower == "bash":
        command = str(tool_input.get("command", ""))
        return extract_bash_subcommands(command)

    if tool_lower in _FILE_TOOLS and field:
        raw_path = str(tool_input.get(field, ""))
        return [_normalize_path(raw_path, working_directory)]

    if field:
        return [str(tool_input.get(field, ""))]

    # Unknown / MCP tool — fall back to full JSON
    return [json.dumps(tool_input, ensure_ascii=False)]


def get_param_strings_for_opencode(patterns: list) -> list[str]:
    """Return the list of parameter strings to match for an OpenCode permission event.

    OpenCode already parses compound bash commands via tree-sitter internally,
    producing one pattern per AST command node.  The gateway must require ALL
    patterns to match — not just patterns[0].

    Returns ``[""]`` for an empty patterns list so that a tool-name-only rule
    (``rule.params is None``) still matches correctly.
    """
    return list(patterns) if patterns else [""]


def matches_rule(rule: "ToolRule", tool_name: str, param_string: str) -> bool:
    """Return True if tool_name and param_string both satisfy the rule.

    Both the tool regex and the params regex use case-insensitive fullmatch,
    so the entire string must match (use .* for prefix/suffix flexibility).
    If rule.params is None, only the tool name is checked.
    """
    if not re.fullmatch(rule.tool, tool_name, re.IGNORECASE):
        return False
    if rule.params is not None:
        if not re.fullmatch(rule.params, param_string, re.IGNORECASE | re.DOTALL):
            return False
    return True


def matches_any(rules: "list[ToolRule]", tool_name: str, param_string: str) -> bool:
    """Return True if any rule in the list matches (tool_name, param_string)."""
    return any(matches_rule(r, tool_name, param_string) for r in rules)


def all_params_match_any(
    rules: "list[ToolRule]",
    tool_name: str,
    param_strings: list[str],
) -> bool:
    """Return True if every param string in param_strings matches at least one rule.

    This is the correct auto-approve check when a tool call produces multiple
    parameter strings (e.g. compound bash commands, OpenCode multi-pattern events).
    A single param string that doesn't match any rule is enough to reject.
    """
    return all(matches_any(rules, tool_name, p) for p in param_strings)
