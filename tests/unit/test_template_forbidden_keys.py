"""The template forbidden-key sets must have exactly one definition.

These keys — the ones identifying a single entry, which a shared `*_templates:`
block therefore may not set — were four byte-identical `frozenset` literals plus
a fifth copy in a dict, spread across the loader, the validator and the config
tool. A comment above one of them claimed unit tests kept them in sync "by
importing both from the same source, not by hand". Neither half was true:
nothing imported anything, and no such test existed.

This is that test. It asserts the single-source property structurally — that
every consumer resolves to the same object — rather than re-listing the expected
keys in a second place, which would recreate the problem it exists to prevent.

Run with:
    uv run python -m pytest tests/unit/test_template_forbidden_keys.py -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from gateway.config import TEMPLATE_FORBIDDEN_KEYS

_GATEWAY = Path(__file__).resolve().parents[2] / "gateway"


class TestSingleSource(unittest.TestCase):
    def test_the_config_tool_uses_the_loader_s_table(self):
        from gateway.configtool.model import _TEMPLATES_FORBIDDEN_KEYS

        self.assertIs(
            _TEMPLATES_FORBIDDEN_KEYS,
            TEMPLATE_FORBIDDEN_KEYS,
            "the config tool has its own copy again",
        )

    def test_the_validator_imports_the_loader_s_table(self):
        import gateway.config_validate as cv

        self.assertIs(cv.TEMPLATE_FORBIDDEN_KEYS, TEMPLATE_FORBIDDEN_KEYS)

    def test_every_template_kind_is_covered(self):
        """A kind missing here would silently forbid nothing."""
        from gateway.configtool.model import _TEMPLATES_KEY

        self.assertEqual(set(TEMPLATE_FORBIDDEN_KEYS), set(_TEMPLATES_KEY))

    def test_a_watcher_template_may_not_set_identity_or_room_keys(self):
        """Pins intent, not just structure: these four are what make an entry
        one specific watcher, so a shared block cannot carry them."""
        self.assertEqual(
            TEMPLATE_FORBIDDEN_KEYS["watcher"],
            frozenset({"name", "room", "rooms", "session_id"}),
        )

    def test_a_connector_template_may_not_set_its_name(self):
        self.assertEqual(TEMPLATE_FORBIDDEN_KEYS["connector"], frozenset({"name"}))

    def test_an_agent_template_forbids_nothing(self):
        """Agents are keyed by their mapping key, so no in-entry identity field
        exists to protect."""
        self.assertEqual(TEMPLATE_FORBIDDEN_KEYS["agent"], frozenset())


class TestNoReintroducedLiterals(unittest.TestCase):
    """Guards the property that actually decayed: a copy reappearing.

    Scans for a frozenset literal being passed straight to
    _parse_templates_block, which is how all four copies existed. Structural
    (AST), so it does not trip over comments or docstrings mentioning the keys.
    """

    def _offending_calls(self, path: Path) -> list[int]:
        tree = ast.parse(path.read_text())
        bad: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "_parse_templates_block":
                continue
            for arg in node.args:
                # frozenset(...) built inline at the call site
                if (
                    isinstance(arg, ast.Call)
                    and getattr(arg.func, "id", "") == "frozenset"
                ):
                    bad.append(node.lineno)
        return bad

    def test_no_module_passes_an_inline_frozenset(self):
        offenders: dict[str, list[int]] = {}
        for path in _GATEWAY.rglob("*.py"):
            lines = self._offending_calls(path)
            if lines:
                offenders[str(path.relative_to(_GATEWAY.parent))] = lines

        self.assertEqual(
            offenders,
            {},
            "a forbidden-key set is being built at the call site again — import "
            "TEMPLATE_FORBIDDEN_KEYS from gateway.config instead",
        )


if __name__ == "__main__":
    unittest.main()
