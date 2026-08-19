"""Unit tests for gateway/configtool/screens/form_common.py's standalone
helpers — find_referencing_watcher_labels() specifically, since it's the
basis for the pre-delete "still used by watcher(s): ..." check on both
AgentDetailScreen and ConnectorDetailScreen.

Rewritten with the Rules tab: a `watchers:` entry is a RULE with a required
unique name, so labels are the rules' own names (position fallback for a
malformed nameless entry) and matching walks the raw entries' MERGED view —
it no longer needs the whole config to load, which the old
expanded-watchers implementation did. That old implementation returned []
for every rule (rules never expanded), silently unblocking the deletion of
a connector every rule referenced — the regression this suite now pins
against.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.configtool.model import EditableConfig
from gateway.configtool.screens.form_common import (
    FieldSpec,
    find_referencing_watcher_labels,
    list_to_text,
    read_widget_value,
    round_trip_value,
)


class TestFindReferencingWatcherLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agent_dir = self.tmp / "work"
        self.agent_dir.mkdir()

    def _cfg(self, yaml_text: str) -> EditableConfig:
        path = self.tmp / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return EditableConfig.load(path)

    def _base(self, watchers_yaml: str, extra_top: str = "") -> EditableConfig:
        return self._cfg(f"""\
            {extra_top}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
{watchers_yaml}
        """)

    def test_finds_a_rule_by_explicit_connector(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_finds_a_rule_by_explicit_agent(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="default"), ["my-rule"])

    def test_returns_empty_when_nothing_references_the_name(self):
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="unrelated"), [])
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="unrelated"), [])

    def test_finds_a_rule_that_only_inherits_its_connector_from_a_template(self):
        """A watcher_templates: entry may set connector/agent (unlike
        name/room/rooms/session_id) — a rule with no explicit 'connector:'
        of its own, only inheriting one via 'inherits:', still counts as
        referencing it (checked against the MERGED view)."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                inherits: standard\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n",
            extra_top="watcher_templates:\n              standard:\n                connector: rc\n",
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_a_nameless_malformed_entry_falls_back_to_its_position_label(self):
        """A rule's name is required — a raw entry without one is malformed
        (the loader refuses it), but if it names the connector, deleting
        that connector still deserves a block with SOME label."""
        cfg = self._base(
            "              - connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["watchers[0]"])

    def test_the_position_label_uses_the_unfiltered_document_index(self):
        """Internal review (lens A): the `watchers[i]` fallback used to be
        numbered over the FILTERED dict-only list, so a non-mapping entry
        earlier in `watchers:` made the label disagree with every other
        consumer of that spelling — the Rules tab's row numbers and the
        validator's own `(index i)`/`watchers[i]` attributions all number
        the unfiltered document list."""
        cfg = self._base(
            "              - \"garbage string, not a mapping\"\n"
            "              - connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["watchers[1]"])

    def test_a_broken_config_still_blocks_on_explicit_references(self):
        """Regression: the old expanded-watchers implementation returned []
        whenever the config didn't fully load, so deleting a connector that
        a (broken) rule explicitly referenced went unblocked. Matching is
        raw-entry-based now — an unrelated breakage elsewhere must not
        silently unblock this deletion."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                connector: rc\n"
            "                agent: nonexistent-agent\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_a_rule_relying_on_the_loader_fallback_blocks_the_fallback_connector(self):
        """Codex review of #129: a rule with no connector anywhere resolves
        to the loader's fallback — the FIRST connector in document order —
        and deleting that connector leaves the config VALID (the fallback
        silently rebinds to the next connector), so save()'s gate never
        blocks it. The pre-check must therefore resolve the same fallback
        the loader does and block the deletion."""
        cfg = self._base(
            "              - name: my-rule\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
        )
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_the_fallback_rule_does_not_block_a_non_first_connector(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
              - name: rc2
                type: rocketchat
                server: {{url: http://localhost:3001, username: bot2, password: pw2}}
            watchers:
              - name: my-rule
                agent: default
                rooms:
                  include: [general]
        """)
        # The fallback is connectors[0] ('rc') — rc2 is untouched by it.
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc2"), [])
        self.assertEqual(find_referencing_watcher_labels(cfg, connector_name="rc"), ["my-rule"])

    def test_the_agent_fallback_honors_an_explicit_default_agent(self):
        """The loader's agent fallback is `default_agent:` when set, the
        first agent otherwise — a rule with no agent anywhere must block
        the deletion of THAT agent, not whichever happens to be first."""
        cfg = self._cfg(f"""\
            default_agent: other
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
              other:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: my-rule
                connector: rc
                rooms:
                  include: [general]
        """)
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="other"), ["my-rule"])
        self.assertEqual(find_referencing_watcher_labels(cfg, agent_name="default"), [])

    def test_multiple_referencing_rules_are_all_returned(self):
        cfg = self._base(
            "              - name: rule-a\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [general]\n"
            "              - name: rule-b\n"
            "                connector: rc\n"
            "                agent: default\n"
            "                rooms:\n"
            "                  include: [dev]\n"
        )
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc"), ["rule-a", "rule-b"]
        )

    def test_both_connector_and_agent_filters_must_match(self):
        cfg = self._cfg(f"""\
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
              other:
                type: claude
                working_directory: {self.agent_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            watchers:
              - name: rule-a
                connector: rc
                agent: other
                rooms:
                  include: [general]
        """)
        # connector matches but agent doesn't -> no match
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="default"), []
        )
        self.assertEqual(
            find_referencing_watcher_labels(cfg, connector_name="rc", agent_name="other"),
            ["rule-a"],
        )


if __name__ == "__main__":
    unittest.main()


class TestListToTextTolerance(unittest.TestCase):
    """A "list"-kind field's box must render ANY on-disk value without
    crashing and without silently rewriting it (Codex review of #129,
    round 4 — both failures pre-existed this branch, and the same pair is
    documented at the loader in `_resolve_paths`)."""

    def test_a_real_list_is_joined(self):
        self.assertEqual(list_to_text(["a", "b"]), "a, b")

    def test_an_empty_or_absent_value_is_blank(self):
        self.assertEqual(list_to_text([]), "")
        self.assertEqual(list_to_text(None), "")

    def test_a_truthy_non_iterable_renders_as_one_item_not_a_typeerror(self):
        """`rooms.include: 5` used to raise TypeError mid-compose, taking
        the TUI down on a row the validator had just invited the operator
        to repair."""
        self.assertEqual(list_to_text(5), "5")

    def test_a_bare_string_is_one_item_not_one_item_per_character(self):
        """`context_inject_files: notes.md` used to display
        'n, o, t, e, s, ., m, d' — and saving that box wrote eight bogus
        one-character paths over the operator's one real value."""
        self.assertEqual(list_to_text("notes.md"), "notes.md")


class _Shim:
    def __init__(self, value):
        self.value = value


class TestRoundTripValueEnumeratesEveryKind(unittest.TestCase):
    """The snapshot a form diffs against must be what a freshly-composed
    widget reads back, or an untouched field looks edited and Save rewrites
    it. Two rounds of review each found another value shape where the two
    disagreed, so this enumerates the surface: for EVERY field kind, the
    round-tripped snapshot must be a fixed point of the widget's own
    read-back (Codex review of #129, rounds 4-5)."""

    # (spec, on-disk value) pairs covering each kind plus the shapes that
    # actually broke: a quoted number, a bare string in a list field, a
    # list item containing the join delimiter, falsy-but-present values.
    CASES = [
        (FieldSpec("s", "str", "S"), "hello"),
        (FieldSpec("s", "str", "S"), ""),
        (FieldSpec("s", "str", "S"), None),
        (FieldSpec("i", "int", "I"), 15),
        (FieldSpec("i", "int", "I"), 0),
        (FieldSpec("i", "int", "I"), "15"),          # quoted number
        (FieldSpec("i", "int", "I"), None),
        (FieldSpec("f", "float", "F"), 1.5),
        (FieldSpec("f", "float", "F"), 0.0),
        (FieldSpec("f", "float", "F"), "1.5"),       # quoted number
        (FieldSpec("b", "bool", "B"), True),
        (FieldSpec("b", "bool", "B"), False),
        (FieldSpec("b", "bool", "B"), None),
        (FieldSpec("l", "list", "L"), ["a", "b"]),
        (FieldSpec("l", "list", "L"), []),
        (FieldSpec("l", "list", "L"), None),
        (FieldSpec("l", "list", "L"), "notes.md"),   # bare string
        (FieldSpec("l", "list", "L"), ["team,one"]),  # delimiter in an item
        (FieldSpec("l", "list", "L"), 5),            # truthy non-iterable
        (FieldSpec("e", "enum", "E", options=("x", "y")), "y"),
        (FieldSpec("e", "enum", "E", options=("x", "y")), "nope"),
    ]

    def test_every_kind_and_shape_is_a_fixed_point_of_the_widget_readback(self):
        for spec, raw in self.CASES:
            with self.subTest(kind=spec.kind, raw=raw):
                snapshot = round_trip_value(spec, raw)
                # What the composed widget shows for that snapshot, read back
                # exactly as _collect_field_updates() would read it.
                if spec.kind == "bool":
                    readback = read_widget_value(spec, _Shim(bool(snapshot)))
                elif spec.kind == "enum":
                    readback = read_widget_value(spec, _Shim(snapshot))
                elif spec.kind == "list":
                    readback = read_widget_value(spec, _Shim(list_to_text(snapshot)))
                else:
                    readback = read_widget_value(
                        spec, _Shim("" if snapshot is None else str(snapshot))
                    )
                self.assertEqual(
                    readback, snapshot,
                    f"{spec.kind} field with {raw!r} on disk would read as "
                    f"{readback!r} against a snapshot of {snapshot!r} — an "
                    "untouched field that Save would rewrite",
                )

    def test_an_unparseable_number_is_kept_raw_rather_than_invented(self):
        """Save is refused loudly on it either way; inventing a value here
        would be the silent rewrite this exists to stop."""
        self.assertEqual(round_trip_value(FieldSpec("i", "int", "I"), "abc"), "abc")
