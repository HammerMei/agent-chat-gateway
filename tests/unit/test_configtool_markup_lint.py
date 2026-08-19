"""A static check: every f-string interpolated into a markup-parsing sink
must escape its dynamic parts.

Why static, on top of the behavioural sweep in
`test_configtool_markup_safety.py`: that suite walks screens, so it can only
catch a site some keystroke actually reaches. Three separate Codex rounds on
PR #129 found unescaped sites, and the last of them (`MessageModal(str(exc))`
on ten paths, the save/create collision modals) sat behind failure branches
no walk-through naturally visits — I had also *excluded* `str(exc)` from my
own grep sweep by hand, on the wrong assumption that an exception message is
ours rather than a quote of the operator's config.

So this reads the source instead of running it, and fails when a NEW
unescaped interpolation appears. The two instruments are complementary:
this one is exhaustive over f-strings but blind to values assembled
elsewhere; the behavioural one covers assembled content but only on paths it
walks.

To satisfy it, either wrap the value — `markup_safe(x)`,
`gateway/configtool/formatting.py` — or, if the value provably cannot carry
operator text, add its exact expression source to `_NOT_OPERATOR_DATA` below
with a reason. Growing that list is the deliberate, reviewable step; missing
an escape silently is what this exists to prevent.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_CONFIGTOOL = Path(__file__).resolve().parents[2] / "gateway" / "configtool"

# Callables whose text Rich/Textual parses as markup. `notify` is absent on
# purpose: `ConfigToolApp.notify()` forces `markup=False` for the whole
# package (see its docstring), so toasts are plain text by construction.
_SINK_NAMES = frozenset(
    {
        "MessageModal",
        "ConfirmModal",
        "Static",
        "Label",
        "update",
        "add_row",
        "add_column",
    }
)

# Expressions that cannot carry operator-authored text, so they need no
# escaping. Each entry is the exact source of an interpolated expression.
_NOT_OPERATOR_DATA = frozenset(
    {
        # Literal-typed values from closed sets declared in this package.
        "kind",
        "self.kind",
        "self._entity_noun()",
        "self._delete_blocker_noun()",
        # Counts and positions computed here, never read from config.
        "index + 1",
        "len(rules)",
        "inherit_count",
        "override_count",
        "row",
        "i",
        # Already-escaped composites assembled immediately above the sink
        # (each is built from markup_safe() parts at its own site).
        "type_suffix",
        "prov_text",
        "blast_text",
        # A Python type NAME, from type(...).__name__ — never a value.
        "type(existing).__name__",
        # Section headings taken from literal tuples declared at the call
        # site (the two tool-list headings, and the picker's "(current)"
        # marker) — no config value can reach them.
        "label",
        "suffix",
    }
)


def _iter_python_files():
    return sorted(_CONFIGTOOL.rglob("*.py"))


def _sink_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# Keyword arguments that are NOT rendered content: widget identity, CSS and
# row keys. `title=` is deliberately absent — MessageModal renders its title
# through a Static, so a title is content like any other.
_NON_CONTENT_KEYWORDS = frozenset({"id", "classes", "name", "key", "placeholder"})


def _is_escaped(node: ast.expr, source: str) -> bool:
    """True when this interpolated expression escapes what it renders.

    A direct `markup_safe(x)` is the common case. The looser test — the
    expression's source mentioning `markup_safe(` at all — covers the join
    idiom (`", ".join(markup_safe(b) for b in blockers)`), where the escape
    is applied per item inside a comprehension. Loose on purpose: one
    interpolation is one expression, so a `markup_safe(` inside it is
    escaping the value being rendered rather than some bystander.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "markup_safe"
    ):
        return True
    segment = ast.get_source_segment(source, node) or ""
    return "markup_safe(" in segment


def _unescaped_interpolations(path: Path) -> list[tuple[int, str, str]]:
    """(line, sink, expression source) for every unescaped dynamic part of an
    f-string passed to a markup sink in `path`."""
    source = path.read_text()
    tree = ast.parse(source)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        sink = _sink_name(node)
        if sink not in _SINK_NAMES:
            continue
        args = list(node.args) + [
            kw.value
            for kw in node.keywords
            if kw.arg not in _NON_CONTENT_KEYWORDS
        ]
        for arg in args:
            if not isinstance(arg, ast.JoinedStr):
                continue  # assembled elsewhere: the behavioural suite's job
            for part in arg.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                if _is_escaped(part.value, source):
                    continue
                expr = ast.get_source_segment(source, part.value) or ast.dump(part.value)
                if expr.strip() in _NOT_OPERATOR_DATA:
                    continue
                findings.append((part.lineno, sink, expr.strip()))
    return findings


class TestNoUnescapedMarkupInterpolations(unittest.TestCase):
    def test_every_fstring_into_a_markup_sink_escapes_its_values(self):
        offenders: list[str] = []
        for path in _iter_python_files():
            for line, sink, expr in _unescaped_interpolations(path):
                rel = path.relative_to(_CONFIGTOOL.parents[1])
                offenders.append(f"{rel}:{line}  {sink}(... {{{expr}}} ...)")
        self.assertEqual(
            offenders,
            [],
            "unescaped interpolation into a markup-parsing sink — wrap it in "
            "markup_safe(), or add the expression to _NOT_OPERATOR_DATA with a "
            "reason if it provably cannot carry operator text:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_checker_actually_detects_an_unescaped_value(self):
        """A guard on the guard: a checker that silently matches nothing
        would pass this suite forever while enforcing nothing."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                'name = "x"\n'
                'MessageModal(f"A rule named {name} exists.")\n'
                'Static(f"safe: {markup_safe(name)}")\n'
            )
            found = _unescaped_interpolations(probe)
        self.assertEqual([(2, "MessageModal", "name")], found)


if __name__ == "__main__":
    unittest.main()
