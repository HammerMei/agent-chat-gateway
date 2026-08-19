"""`watchers[].session_id` is removed, and refused rather than ignored.

Pinning a session from config is gone for two reasons, neither of them a rename:

* A pinned id names a session the backend is free to expire — Claude Code's default
  `cleanupPeriodDays` is 30 — after which the id refers to nothing and the watcher
  starts empty with no warning.
* With a watcher created per discovered room, a single id in config cannot say which
  room it belongs to.

**Why refusing matters more here than for the fields #97 moved.** Every field on a
watcher entry is read with `.get()`, and unknown keys are deliberately ignored so
`description:` and friends need no handling — pinned by
`test_agent_and_watcher_description_do_not_break_loading`. So deleting this field
quietly would not have raised anything: the config would still load, the session
would simply stop being pinned, and the operator would discover it from the agent's
missing memory. And unlike the TTLs, `session_id` **shipped** — it is documented in
`v0.5.1`'s own `config.example.yaml`, so silence would have landed on real
deployments. The JSON schema does not cover this either: it is not enforced at load.

Landed in two PRs. The first refused the key while keeping
`WatcherConfig.session_id` as a field that could only ever be `None`, which made the
runtime's pinned-session branches unreachable; the second removed those branches and
the field. Splitting it that way is what let the runtime removal be argued as dead
code rather than as a behaviour change.

Run with:
    uv run python -m pytest tests/unit/test_session_id_removed.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest

from gateway.config import GatewayConfig, collect_config
from gateway.config_validate import validate_config


def write_config(watchers_block: str, extra: str = "") -> str:
    body = (
        "connectors:\n"
        "  - name: rc\n"
        "    type: rocketchat\n"
        "    server: {url: http://localhost:3000, username: bot, password: pw}\n"
        "agents:\n"
        "  default:\n"
        "    type: claude\n"
        "    working_directory: /tmp\n"
        + extra
        + "watchers:\n"
        + textwrap.indent(textwrap.dedent(watchers_block), "  ")
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


class TestTheKeyIsRefusedNotIgnored(unittest.TestCase):
    """The whole point of the change: silence was the available failure mode."""

    def test_a_pinned_id_is_a_load_error(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- {name: w1, rooms: {include: [general]}, session_id: "ses_abc123"}\n'))
        msg = str(cm.exception)
        self.assertIn("'session_id' is no longer supported", msg)

    def test_the_error_names_the_handoff_replacement(self):
        """There is no replacement *field*, so the error has to describe the
        replacement *mechanism* or it reads as an arbitrary removal."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- {name: w1, rooms: {include: [general]}, session_id: "x"}\n'))
        msg = str(cm.exception)
        self.assertIn("summarise", msg)
        self.assertIn("docs/user-guide.md", msg)

    def test_every_value_shape_is_refused_including_null(self):
        """`null` used to mean "auto-create", i.e. the default — so it is the one
        value an operator might expect to keep working. It does not: writing the key
        at all means they believe pinning exists."""
        for value in ('"ses_abc"', "null", "false", "0", "[]", "{}"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    GatewayConfig.from_file(
                        write_config(f"- {{name: w1, rooms: {{include: [general]}}, session_id: {value}}}\n")
                    )
                self.assertIn("no longer supported", str(cm.exception))

    def test_an_unknown_key_is_refused_because_rules_are_closed(self):
        """The static parser ignored unknown keys, which is why session_id
        needed its own refusal. The rule shape is a closed key set — every
        unknown key raises — so the dedicated message above is kept only
        because it names the replacement mechanism, not to make the key loud."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config("- {name: w1, rooms: {include: [general]}, some_future_key: 7}\n")
            )
        self.assertIn("unknown key(s)", str(cm.exception))

    def test_a_config_without_it_is_unaffected(self):
        cfg = GatewayConfig.from_file(write_config("- {name: w1, rooms: {include: [general]}}\n"))
        self.assertEqual([r.name for r in cfg.watcher_rules], ["w1"])

    def test_a_materialized_watcher_has_no_such_attribute(self):
        """The field is gone from `WatcherConfig`, not merely always None — so the
        runtime cannot read it even by accident. The config half of this change kept
        it as an always-None field so the runtime branches could be removed
        separately; this asserts the second half landed."""
        cfg = GatewayConfig.from_file(write_config("- {name: w1, rooms: {include: [general]}}\n"))
        self.assertFalse(hasattr(cfg.watcher_rules[0], "session_id"))


class TestItCannotArriveByInheritance(unittest.TestCase):
    """A template is merged into the entry before the entry is parsed, so a template
    is the one place a removed key could still slip through."""

    def test_a_template_setting_it_is_refused_and_the_template_is_named(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {name: w1, rooms: {include: [general]}, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("watcher_templates['standard']", msg)
        self.assertIn("session_id", msg)

    def test_the_template_error_states_the_replacement_itself(self):
        """It must not defer to "the per-entry error": this branch raises BEFORE
        inheritance, so that error never runs for a template-supplied key. Pointing at
        an error the operator will never see is worse than saying nothing."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {name: w1, rooms: {include: [general]}, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("summarise", msg)
        self.assertIn("docs/user-guide.md", msg)
        self.assertNotIn("per-entry error", msg)

    def test_the_template_error_does_not_tell_you_to_move_it_per_entry(self):
        """`session_id` stays in TEMPLATE_FORBIDDEN_KEYS so the template is named
        rather than every entry inheriting it — but the generic wording there ("set it
        per-entry") is *wrong* for a field nothing accepts, and a correct attribution
        carrying a wrong instruction is worse than no detail. So the removed keys are
        described separately."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {name: w1, rooms: {include: [general]}, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("does not exist at all any more", msg)
        self.assertNotIn("must be set per-entry", msg)

    def test_an_identity_key_still_gets_the_per_entry_wording(self):
        """The other keys in that set are not removed, so their advice must survive."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {name: w1, rooms: {include: [general]}, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    name: shared\n",
            ))
        msg = str(cm.exception)
        self.assertIn("must be set per-entry", msg)
        self.assertNotIn("does not exist at all", msg)

    def test_a_template_setting_both_kinds_explains_each(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {name: w1, rooms: {include: [general]}, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    name: shared\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("must be set per-entry", msg)
        self.assertIn("does not exist at all any more", msg)


class TestTheRoomCountNoLongerEntersIntoIt(unittest.TestCase):
    def test_it_is_refused_however_many_rooms_the_rule_names(self):
        """The old rule was "only settable with exactly one room"; a rule names
        any number of rooms and the key is refused regardless."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                '- {name: w1, rooms: {include: [a, b]}, session_id: "x"}\n'))
        self.assertIn("no longer supported", str(cm.exception))


class TestThroughTheFaultTolerantLoader(unittest.TestCase):
    def test_it_is_attributed_and_later_entries_still_parse(self):
        cfg, issues = collect_config(write_config("""\
            - {name: pinned, rooms: {include: [general]}, session_id: "ses_abc"}
            - {name: fine, rooms: {include: [dev]}}
            """))
        self.assertEqual([(i.entity_kind, i.entity_name) for i in issues],
                         [("watcher", "pinned")])
        self.assertEqual([r.name for r in cfg.watcher_rules], ["fine"])

    def test_validate_config_reports_it_as_an_error(self):
        result = validate_config(write_config('- {name: w1, rooms: {include: [general]}, session_id: "x"}\n'))
        self.assertFalse(result.ok)
        self.assertTrue(
            any("no longer supported" in e for e in result.errors), result.errors
        )

    def test_the_duplicate_pass_is_gone_rather_than_silently_passing(self):
        """Two watchers sharing one id used to be a dedicated cross-watcher check.
        Both entries are now refused individually, which is strictly stronger — and
        this asserts the old hazard cannot reappear as "no issues at all"."""
        cfg, issues = collect_config(write_config("""\
            - {name: w1, rooms: {include: [general]}, session_id: same}
            - {name: w2, rooms: {include: [dev]}, session_id: same}
            """))
        self.assertEqual(len(issues), 2, [i.message for i in issues])
        self.assertEqual(cfg.watcher_rules, [])


class TestTheRuleShapeIsUnchanged(unittest.TestCase):
    """The rule parser already refused `session_id`; this PR must not disturb it."""

    def test_a_rule_still_gets_the_handoff_error(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config("""\
                - name: eng
                  rooms: {include: [eng-x]}
                  session_id: "x"
                """))
        msg = str(cm.exception)
        self.assertIn("Watcher rule at index 0", msg)
        self.assertIn("session_id", msg)


if __name__ == "__main__":
    unittest.main()
