"""No module in `gateway/` may define the same top-level name twice.

A duplicate module-level `def`, `class`, or annotated constant is silent: Python
binds the later one and the earlier one becomes unreachable, with no error, no
warning, and — verified — no lint finding. It is the failure mode a *clean* merge
produces, which is why it needs its own guard.

The concrete incident: the static and rule watcher parsers each grew a copy of
`_parse_history_handoff`, `_validated_notification`, `_key_list` and
`_HH_FIELD_TYPES`, in different parts of `gateway/config.py`. Merging the two
branches was textually clean — different regions, no conflict — and produced a file
with four shadowed pairs. Every test passed, because assertions matched substrings
of message *bodies*. `ruff check --select F811` on that exact file reported nothing
(the rule fires on a minimal two-line reproduction, so it is enabled and working —
it simply does not fire here; the suppressing condition is unidentified). The only
symptom was a doubled prefix in an error message nothing asserted on.

This test is the cheap systematic answer, in the same spirit as
`TestEveryRuleFieldIsTypeChecked`: enumerate the surface so the next instance fails
locally rather than reaching a reviewer, or nobody.
"""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[2] / "gateway"


def _duplicate_top_level_names(source: str) -> dict[str, list[str]]:
    """Names bound more than once at module level, by kind.

    Plain `Assign` is deliberately NOT checked. Declaring a constant and populating
    it further down is a legitimate idiom this package uses — `gateway/core/config.py`
    declares `_BUILTIN_OWNER_TOOL_RULES: "list[ToolRule]" = []` before `ToolRule`
    exists and fills it in afterwards. A name annotated *twice*, by contrast, is two
    declarations of one thing, which is the shadowing this guards against.
    """
    tree = ast.parse(source)
    defs: list[str] = []
    annotated: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotated.append(node.target.id)

    out: dict[str, list[str]] = {}
    for kind, names in (("def/class", defs), ("annotated constant", annotated)):
        dupes = sorted(name for name, count in Counter(names).items() if count > 1)
        if dupes:
            out[kind] = dupes
    return out


class TestNoShadowedModuleBindings(unittest.TestCase):
    def test_every_gateway_module_binds_each_top_level_name_once(self):
        offenders: dict[str, dict[str, list[str]]] = {}
        scanned = 0
        for path in sorted(GATEWAY.rglob("*.py")):
            scanned += 1
            dupes = _duplicate_top_level_names(path.read_text())
            if dupes:
                offenders[str(path.relative_to(GATEWAY.parent))] = dupes
        self.assertGreater(scanned, 50, "the sweep found almost no modules — bad root?")
        self.assertEqual(
            offenders,
            {},
            "shadowed top-level binding(s): the later definition silently wins, so "
            "the earlier one is dead code and any call site written against it now "
            "runs the wrong implementation",
        )

    def test_the_check_detects_a_shadowed_def(self):
        """Without this, a broken detector would report a clean sweep forever."""
        self.assertEqual(
            _duplicate_top_level_names("def f():\n    pass\n\n\ndef f():\n    pass\n"),
            {"def/class": ["f"]},
        )

    def test_the_check_detects_a_redeclared_annotated_constant(self):
        self.assertEqual(
            _duplicate_top_level_names("X: int = 1\nX: int = 2\n"),
            {"annotated constant": ["X"]},
        )

    def test_declare_then_populate_is_not_flagged(self):
        """The `gateway/core/config.py` forward-declaration idiom must stay legal."""
        self.assertEqual(
            _duplicate_top_level_names('X: "list[int]" = []\nX = [1, 2]\n'),
            {},
        )

    def test_a_nested_redefinition_is_not_flagged(self):
        """Only module level is checked — a local name reused inside two different
        functions is unrelated, and flagging it would make the guard noisy enough to
        be turned off."""
        self.assertEqual(
            _duplicate_top_level_names(
                "def a():\n    def h():\n        pass\n\n\ndef b():\n    def h():\n        pass\n"
            ),
            {},
        )
