"""Provenance for a nested field is its own, because the merge is per sub-key.

`_deep_merge` recurses whenever both sides hold a dict (gateway/config.py), so an
entry that sets `rooms.direct` over a template's `rooms.include` really does
inherit the include. Provenance was computed per whole top-level field, which
reported one verdict for every sub-key of the block — every field under a partly
set `rooms:`/`permissions:`/`server:` read `(explicit)`, whatever the template
supplied, and a ctrl+r on one of them could not change that while any sibling
remained.

The old granularity was justified in the `Provenance` docstring as matching
"`_deep_merge` treating nested dicts as a single mergeable unit". That was never
true of the merge; `_deep_merge`'s own body is the counter-example, and
`test_the_merge_really_is_per_subkey` below asserts it rather than citing it.

Run with:
    uv run python -m pytest tests/unit/test_configtool_subkey_provenance.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest

from gateway.config import _deep_merge
from gateway.configtool.model import EditableConfig, Provenance


def write(body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent(body))
        return f.name


def rule_config(template_rooms: str, entry_rooms: str) -> EditableConfig:
    """One template, one rule, both free to omit `rooms` entirely (pass "")."""
    body = "watcher_templates:\n  channels:\n    connector: rc\n"
    if template_rooms:
        body += f"    rooms: {template_rooms}\n"
    body += "watcher_rules:\n  - name: r1\n    inherits: channels\n"
    if entry_rooms:
        body += f"    rooms: {entry_rooms}\n"
    return EditableConfig.load(write(body))


def prov(cfg: EditableConfig, field: str) -> Provenance:
    return cfg.field_provenance("watcher", cfg.watchers_raw[0], field)


class TestTheMergeReallyIsPerSubkey(unittest.TestCase):
    """The premise, asserted rather than asserted-about."""

    def test_a_nested_dict_is_merged_key_by_key(self):
        merged = _deep_merge({"rooms": {"include": ["a"], "direct": True}},
                             {"rooms": {"direct": False}})
        self.assertEqual(merged["rooms"]["include"], ["a"], "the untouched sub-key survives")
        self.assertIs(merged["rooms"]["direct"], False, "the set sub-key wins")

    def test_a_nested_list_is_still_replaced_wholesale(self):
        """So a sub-key holding a list stays provenance-binary."""
        merged = _deep_merge({"rooms": {"include": ["a", "b"]}},
                             {"rooms": {"include": ["c"]}})
        self.assertEqual(merged["rooms"]["include"], ["c"])


class TestOneBlockReportsFourDifferentVerdicts(unittest.TestCase):
    """The whole feature in one case — the shape from the owner's report."""

    def setUp(self):
        self.cfg = rule_config(
            "{include: ['*'], direct: true}", "{direct: true, group_direct: true}"
        )

    def test_a_subkey_only_the_template_sets_is_inherited(self):
        self.assertEqual(prov(self.cfg, "rooms.include"), Provenance.INHERITED)

    def test_a_subkey_neither_side_sets_is_default(self):
        self.assertEqual(prov(self.cfg, "rooms.except_for"), Provenance.DEFAULT)

    def test_a_subkey_both_set_is_explicit(self):
        self.assertEqual(prov(self.cfg, "rooms.direct"), Provenance.EXPLICIT)

    def test_a_subkey_only_the_entry_sets_is_explicit(self):
        self.assertEqual(prov(self.cfg, "rooms.group_direct"), Provenance.EXPLICIT)

    def test_the_old_granularity_would_have_said_explicit_for_all_four(self):
        """The regression this file exists for: the top-level key IS explicit
        here, which is why every sub-key used to inherit that verdict."""
        self.assertEqual(prov(self.cfg, "rooms"), Provenance.EXPLICIT)
        self.assertNotEqual(prov(self.cfg, "rooms.include"), prov(self.cfg, "rooms"))


class TestTopLevelKeysAreUnaffected(unittest.TestCase):
    """Non-dotted keys take the original code path byte for byte — agent_detail
    and connector_detail pass plain keys (`type`, `timeout`) and must not move."""

    def test_explicit_inherited_and_default(self):
        cfg = EditableConfig.load(write("""\
            watcher_templates:
              channels:
                connector: rc
                session_idle_days: 30
            watcher_rules:
              - {name: r1, inherits: channels, agent: a}
        """))
        self.assertEqual(prov(cfg, "agent"), Provenance.EXPLICIT)
        self.assertEqual(prov(cfg, "session_idle_days"), Provenance.INHERITED)
        self.assertEqual(prov(cfg, "session_expire_days"), Provenance.DEFAULT)

    def test_an_explicit_null_over_a_template_value_suppresses(self):
        cfg = EditableConfig.load(write("""\
            watcher_templates:
              channels: {connector: rc, session_idle_days: 30}
            watcher_rules:
              - {name: r1, inherits: channels, session_idle_days: null}
        """))
        self.assertEqual(prov(cfg, "session_idle_days"), Provenance.EXPLICIT_SUPPRESSING)


class TestTheEdgeCasesOfANestedKey(unittest.TestCase):
    def test_a_null_subkey_over_a_template_value_suppresses(self):
        cfg = rule_config("{include: ['*'], direct: true}", "{direct: null}")
        self.assertEqual(prov(cfg, "rooms.direct"), Provenance.EXPLICIT_SUPPRESSING)

    def test_a_null_subkey_with_nothing_to_suppress_is_explicit(self):
        cfg = rule_config("{include: ['*']}", "{direct: null}")
        self.assertEqual(prov(cfg, "rooms.direct"), Provenance.EXPLICIT)

    def test_a_null_parent_suppresses_every_subkey_the_template_set(self):
        """`rooms: null` replaces the template's block verbatim — `_deep_merge`
        only recurses when both sides are dicts."""
        cfg = rule_config("{include: ['*'], direct: true}", "null")
        self.assertEqual(prov(cfg, "rooms.include"), Provenance.EXPLICIT_SUPPRESSING)
        self.assertEqual(prov(cfg, "rooms.direct"), Provenance.EXPLICIT_SUPPRESSING)

    def test_under_a_null_parent_a_subkey_the_template_never_set_is_explicit(self):
        """The documented coin-toss: nothing is being suppressed, but the entry
        did write something over the whole block. Pinned so the choice is not
        re-decided silently."""
        cfg = rule_config("{include: ['*']}", "null")
        self.assertEqual(prov(cfg, "rooms.group_direct"), Provenance.EXPLICIT)

    def test_a_malformed_non_dict_parent_is_explicit_not_a_crash(self):
        """A hand-edited `rooms: [general]` (the pre-cutover shape) still has to
        RENDER — the row's Status column is what reports the error."""
        cfg = rule_config("{include: ['*'], direct: true}", "[general]")
        for field in ("rooms.include", "rooms.direct", "rooms.group_direct"):
            with self.subTest(field=field):
                self.assertEqual(prov(cfg, field), Provenance.EXPLICIT)

    def test_a_malformed_template_parent_does_not_crash_either(self):
        cfg = rule_config("[general]", "{direct: true}")
        self.assertEqual(prov(cfg, "rooms.direct"), Provenance.EXPLICIT)
        self.assertEqual(prov(cfg, "rooms.include"), Provenance.DEFAULT,
                         "a non-dict template block supplies no sub-key to inherit")

    def test_an_entry_with_no_template_at_all(self):
        cfg = EditableConfig.load(write("""\
            watcher_rules:
              - {name: r1, rooms: {direct: true}}
        """))
        self.assertEqual(prov(cfg, "rooms.direct"), Provenance.EXPLICIT)
        self.assertEqual(prov(cfg, "rooms.include"), Provenance.DEFAULT)


class TestItGeneralizedBeyondRooms(unittest.TestCase):
    """`rooms` is what exposed this, but the fix is in `field_provenance` — every
    dotted FieldSpec on every screen gets it. One case per other kind, since a
    fix that only reached the watcher screens would look identical from `rooms`."""

    def test_a_connector_server_subkey(self):
        cfg = EditableConfig.load(write("""\
            connector_templates:
              shared:
                server: {url: "http://localhost:3000", team: myteam}
            connectors:
              - name: mm
                type: mattermost
                inherits: shared
                server: {token: abc}
        """))
        entry = cfg.connectors_raw[0]
        self.assertEqual(
            cfg.field_provenance("connector", entry, "server.team"), Provenance.INHERITED
        )
        self.assertEqual(
            cfg.field_provenance("connector", entry, "server.token"), Provenance.EXPLICIT
        )
        self.assertEqual(
            cfg.field_provenance("connector", entry, "server.username"), Provenance.DEFAULT
        )

    def test_an_agent_permissions_subkey(self):
        cfg = EditableConfig.load(write("""\
            agent_templates:
              standard:
                permissions: {enabled: true, timeout: 300}
            agents:
              a:
                inherits: standard
                permissions: {timeout: 60}
        """))
        entry = cfg.agents_raw["a"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "permissions.enabled"), Provenance.INHERITED
        )
        self.assertEqual(
            cfg.field_provenance("agent", entry, "permissions.timeout"), Provenance.EXPLICIT
        )


if __name__ == "__main__":
    unittest.main()
