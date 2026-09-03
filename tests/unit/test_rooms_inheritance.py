"""What a `watcher_templates:` entry can put in `rooms:`, and what happens when it does.

`rooms:` became inheritable when the loader stopped deciding an entry's shape by
looking for a `rooms:` mapping on the RAW entry — a template is merged only after
that decision, so a template-supplied `rooms:` was invisible to it. This file pins
the behaviour that inheritance now has.

**Why these particular cases.** Every one of them is a claim made in
`docs/user-guide.md` §"Templates and `rooms` inheritance", and each assertion here
is the measurement that claim was written from. `rooms:` is a *matcher*, not a
settings block, so the useful-looking thing to share is often the wrong thing to
share: an exclusion every rule should carry works, and a `direct: true` every rule
should carry silently leaves all but the first rule with no DMs. A doc example that
demonstrates the second while claiming the first is the specific failure this file
exists to catch — it has happened, in this section, in both directions.

Run with:
    uv run python -m pytest tests/unit/test_rooms_inheritance.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest

from gateway.config import GatewayConfig
from gateway.config_validate import validate_config

# A local config writer rather than one in tests/helpers.py: what these cases need
# is a `watcher_templates:` block alongside `watcher_rules:`, and no shared builder
# produces one — `make_rule()` builds a parsed rule object, which is downstream of
# the merge under test here. The config suites each write their own YAML for the
# same reason.
BASE = """\
    connectors:
      - name: rc-main
        type: rocketchat
        server: {url: http://localhost:3000, username: bot, password: pw}
    agents:
      claude:
        type: claude
        working_directory: /tmp
"""


def write_config(template_rooms: str | None, rules: str) -> str:
    """A config whose one template carries `rooms: <template_rooms>`."""
    body = textwrap.dedent(BASE)
    # The template carries `agent`, which is now the ONLY way several rules can
    # share one: a rule states its agent or inherits it, with no implicit
    # `default_agent:` fallback left to pick one by document order.
    body += "watcher_templates:\n  channels:\n    connector: rc-main\n    agent: claude\n"
    if template_rooms is not None:
        body += f"    rooms: {template_rooms}\n"
    body += "watcher_rules:\n" + textwrap.indent(textwrap.dedent(rules), "  ")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


def load_rooms(template_rooms: str | None, entry_rooms: str | None):
    """The single rule's resolved matcher, after the template is merged in."""
    entry = "- {name: r1, inherits: channels"
    if entry_rooms is not None:
        entry += f", rooms: {entry_rooms}"
    entry += "}\n"
    config = GatewayConfig.from_file(write_config(template_rooms, entry))
    return config.watcher_rules[0].rooms


def load_error(template_rooms: str | None, entry_rooms: str | None) -> str:
    entry = "- {name: r1, inherits: channels"
    if entry_rooms is not None:
        entry += f", rooms: {entry_rooms}"
    entry += "}\n"
    try:
        GatewayConfig.from_file(write_config(template_rooms, entry))
    except ValueError as exc:
        return " ".join(str(exc).split())
    raise AssertionError("expected a load error, config loaded cleanly")


def raws(patterns) -> list[str]:
    return [p.raw for p in patterns]


class TestEachSubkeyArrivesFromATemplate(unittest.TestCase):
    """All four `rooms` subkeys inherit. Three of them are usable alone."""

    def test_include_alone(self):
        rooms = load_rooms("{include: ['a-*']}", None)
        self.assertEqual(raws(rooms.include), ["a-*"])

    def test_direct_alone(self):
        rooms = load_rooms("{direct: true}", None)
        self.assertTrue(rooms.direct)
        self.assertEqual(raws(rooms.include), [], "a DM opt-in needs no include")

    def test_group_direct_alone(self):
        rooms = load_rooms("{group_direct: true}", None)
        self.assertTrue(rooms.group_direct)

    def test_except_for_alone_matches_nothing_and_is_refused(self):
        """Not an inheritance failure — the merged rule genuinely selects no room.
        `except_for` subtracts from `include`, so on its own there is nothing to
        subtract from and no DM class opted into."""
        msg = load_error("{except_for: ['x-*']}", None)
        self.assertIn("can never match any room", msg)
        self.assertIn("'rooms.include' is empty", msg)


class TestTheRuleMergesOverTheTemplateKeyByKey(unittest.TestCase):
    def test_a_key_the_rule_omits_is_inherited(self):
        """The combination worth sharing a template for: the template contributes
        the DM opt-in, the rule contributes the channel names, and both survive."""
        rooms = load_rooms("{direct: true}", "{include: ['a-*']}")
        self.assertEqual(raws(rooms.include), ["a-*"])
        self.assertTrue(rooms.direct)

    def test_a_list_the_rule_sets_replaces_rather_than_extends(self):
        """The one most likely to be assumed the other way. `_deep_merge` treats a
        list as one value, so a rule wanting both patterns must list both."""
        rooms = load_rooms("{include: ['a-*']}", "{include: ['b-*']}")
        self.assertEqual(raws(rooms.include), ["b-*"])
        self.assertNotIn("a-*", raws(rooms.include))

    def test_replacement_is_per_key_not_whole_block(self):
        """A rule overriding `except_for` keeps the template's `include` — the
        merge is key by key, so setting one list does not discard the other."""
        rooms = load_rooms(
            "{include: ['a-*', 'b-*'], except_for: ['x-*']}", "{except_for: ['a-*']}"
        )
        self.assertEqual(raws(rooms.include), ["a-*", "b-*"], "inherited untouched")
        self.assertEqual(raws(rooms.except_for), ["a-*"], "the rule's list replaced it")

    def test_false_turns_off_an_inherited_flag(self):
        rooms = load_rooms("{direct: true}", "{direct: false, include: ['a-*']}")
        self.assertFalse(rooms.direct)

    def test_null_does_not_turn_off_an_inherited_flag(self):
        """`null` suppresses a key elsewhere in the merge, so it is worth pinning
        that it does NOT work here: the field is read as a boolean and rejects it,
        which is the loud outcome rather than a silently inherited `true`."""
        msg = load_error("{direct: true}", "{direct: null, include: ['a-*']}")
        self.assertIn("'rooms.direct' must be true or false", msg)


class TestAnInheritedExclusionMustBiteOnTheInheritingRule(unittest.TestCase):
    """`except_for` removes rooms from THIS rule's `include`. Inherited, it lands
    next to an `include` the template's author never saw — so the overlap that
    makes it meaningful has to hold for every rule that inherits it."""

    def test_a_suffix_exclusion_overlaps_every_prefix_include(self):
        """Why the documented example uses `*-secret` and not a literal name."""
        rooms = load_rooms("{except_for: ['*-secret']}", "{include: ['eng-*']}")
        self.assertEqual(raws(rooms.except_for), ["*-secret"])
        self.assertEqual(raws(rooms.include), ["eng-*"])

    def test_an_exclusion_that_cannot_overlap_is_a_hard_error(self):
        """It reads as protection and would remove nothing, so it is refused at
        load rather than accepted as a no-op."""
        msg = load_error("{except_for: ['ops-secret']}", "{include: ['eng-*']}")
        self.assertIn("does nothing here", msg)
        self.assertIn("ops-secret", msg)

    def test_the_error_names_the_rule_that_inherited_it(self):
        """Templates are merged before a rule is parsed, so the report is against
        the rule — the template is where the fix goes, but the rule is where the
        contradiction is."""
        msg = load_error("{except_for: ['ops-*']}", "{include: ['eng-*']}")
        self.assertIn("'r1'", msg)


class TestASharedDmOptInStarvesEveryRuleButTheFirst(unittest.TestCase):
    """The anti-pattern. It loads, so only `config validate` can catch it — which
    is why the doc states it as a rule rather than leaving it to the loader."""

    TWO_RULES = """\
        - {name: eng, inherits: channels, rooms: {include: ['eng-*']}}
        - {name: ops, inherits: channels, rooms: {include: ['ops-*']}}
    """

    def test_two_rules_inheriting_direct_produce_a_shadow_warning(self):
        result = validate_config(write_config("{direct: true}", self.TWO_RULES))
        self.assertTrue(result.ok, "it loads — nothing here is a load error")
        dm_warnings = [w for w in result.warnings if "direct messages" in w]
        self.assertTrue(dm_warnings, result.warnings)
        self.assertIn("'ops'", dm_warnings[0], "the starved rule is named")
        self.assertIn("'eng'", dm_warnings[0], "so is the rule taking the DMs")

    def test_a_shared_exclusion_over_the_same_two_rules_is_clean(self):
        """The contrast that makes the warning above meaningful: the exact same
        two rules over the documented template raise nothing at all."""
        result = validate_config(write_config("{except_for: ['*-secret']}", self.TWO_RULES))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.warnings, [], result.warnings)

    def test_a_dm_rule_outside_the_template_is_the_documented_fix(self):
        """`dms` does not inherit — so it is not competing with `eng`/`ops`, and it
        does not pick up an `except_for` it has no `include` to subtract from."""
        rules = (
            textwrap.dedent(self.TWO_RULES)
            # Its own `agent:`, because it inherits nothing — the price of
            # staying out of the template, and cheaper than the DM starvation
            # inheriting it would cause.
            + "- {name: dms, connector: rc-main, agent: claude, rooms: {direct: true}}\n"
        )
        result = validate_config(write_config("{except_for: ['*-secret']}", rules))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.warnings, [], result.warnings)


if __name__ == "__main__":
    unittest.main()
