"""`watchers[].session_id` is removed, and refused — as an unknown key, not by name.

Pinning a session from config is gone for two reasons, neither of them a rename:

* A pinned id names a session the backend is free to expire — Claude Code's default
  `cleanupPeriodDays` is 30 — after which the id refers to nothing and the watcher
  starts empty with no warning.
* With a watcher created per discovered room, a single id in config cannot say which
  room it belongs to.

**Why this file no longer pins a dedicated message.** It used to, and the reason
it gave was sound at the time: the static watcher parser read every field with
`.get()` and ignored what it did not recognise, so removing `session_id` quietly
would have loaded a config that silently stopped pinning anything. That premise
expired when the rule shape became a **closed key set** — every key a rule does
not declare is already a load error, which is what `test_an_unknown_key_is_refused`
below asserts. Once that was true, a removed field earned nothing from its own
rejection path, so the special case is gone and `session_id` is reported the way
`sesion_id` or any other non-key is.

One thing was traded away deliberately, and it is worth knowing rather than
rediscovering: a `session_id` inside a `watcher_templates:` entry is now reported
against the ENTRY that inherits it rather than against the template that carries
it, because templates are merged before an entry is parsed. That is how every
other unknown key already behaved; the previous behaviour was one field's
exception to it.

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
        + "watcher_rules:\n"
        + textwrap.indent(textwrap.dedent(watchers_block), "  ")
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


class TestTheKeyIsRefusedNotIgnored(unittest.TestCase):
    """Loud, still — just by the general rule rather than a special case."""

    def test_a_pinned_id_is_a_load_error(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config('- {name: w1, rooms: {include: [general]}, session_id: "x", agent: default}\n')
            )
        msg = str(cm.exception)
        self.assertIn("Watcher rule at index 0", msg, "the entry is named")
        self.assertIn("session_id", msg, "the offending key is named")
        self.assertIn("unknown key(s)", msg)

    def test_the_error_lists_the_keys_a_rule_does_accept(self):
        """What replaced the hand-written message: the closed key set prints
        itself, so a reader sees `rooms`, `agent`, `history_handoff` and the rest
        without anyone maintaining a sentence about it."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config('- {name: w1, rooms: {include: [general]}, session_id: "x", agent: default}\n')
            )
        msg = str(cm.exception)
        self.assertIn("Valid keys are", msg)
        self.assertIn("'rooms'", msg)
        self.assertNotIn("'session_id'", msg.split("Valid keys are", 1)[1])

    def test_every_value_shape_is_refused_including_null(self):
        """`null` is not a way to keep the key: writing it at all means the
        operator believes an entry can choose its conversation."""
        for value in ('"x"', "null", "123", "[a]", "{a: b}"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    GatewayConfig.from_file(
                        write_config(
                            f"- {{name: w1, rooms: {{include: [general]}}, session_id: {value}}}\n"
                        )
                    )
                self.assertIn("session_id", str(cm.exception))

    def test_an_unknown_key_is_refused(self):
        """The mechanism this file now rests on. The static parser ignored
        unknown keys, which is why `session_id` once needed its own refusal; the
        rule shape is a closed key set, so it does not."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config("- {name: w1, rooms: {include: [general]}, some_future_key: 7, agent: default}\n")
            )
        self.assertIn("unknown key(s)", str(cm.exception))

    def test_a_config_without_it_is_unaffected(self):
        cfg = GatewayConfig.from_file(write_config("- {name: w1, rooms: {include: [general]}, agent: default}\n"))
        self.assertEqual([r.name for r in cfg.watcher_rules], ["w1"])

    def test_a_materialized_watcher_has_no_such_attribute(self):
        """The field is gone from `WatcherConfig`, not merely always None — so the
        runtime cannot read it even by accident."""
        cfg = GatewayConfig.from_file(write_config("- {name: w1, rooms: {include: [general]}, agent: default}\n"))
        self.assertFalse(hasattr(cfg.watcher_rules[0], "session_id"))


class TestItCannotArriveByInheritance(unittest.TestCase):
    """A template cannot smuggle it in — the merged entry is what gets parsed."""

    def test_a_template_setting_it_is_still_refused(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config(
                    "- {name: w1, inherits: shared, rooms: {include: [general]}, agent: default}\n",
                    extra='watcher_templates:\n  shared:\n    session_id: "x"\n',
                )
            )
        self.assertIn("session_id", str(cm.exception))
        self.assertIn("unknown key(s)", str(cm.exception))

    def test_an_identity_key_in_a_template_is_a_different_error(self):
        """`name` is still rejected by the template loader itself, and that error
        names the TEMPLATE — the distinction `session_id` used to share and no
        longer does. (`rooms` was the other example here until it became
        inheritable.)"""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(
                write_config(
                    "- {name: w1, inherits: shared, rooms: {include: [general]}, agent: default}\n",
                    extra="watcher_templates:\n  shared:\n    name: nope\n",
                )
            )
        msg = str(cm.exception)
        self.assertIn("watcher_templates['shared']", msg, "the template is named")
        self.assertIn("names one specific entry", msg)


class TestThroughTheFaultTolerantLoader(unittest.TestCase):
    def test_it_is_attributed_and_later_entries_still_parse(self):
        cfg, issues = collect_config(write_config("""\
            - {name: pinned, rooms: {include: [general]}, session_id: "ses_abc", agent: default}
            - {name: fine, rooms: {include: [dev]}, agent: default}
            """))
        self.assertEqual([(i.entity_kind, i.entity_name) for i in issues],
                         [("watcher", "pinned")])
        self.assertEqual([r.name for r in cfg.watcher_rules], ["fine"])

    def test_validate_config_reports_it_as_an_error(self):
        result = validate_config(write_config('- {name: w1, rooms: {include: [general]}, session_id: "x", agent: default}\n'))
        self.assertFalse(result.ok)
        self.assertTrue(
            any("unknown key(s)" in e and "session_id" in e for e in result.errors),
            result.errors,
        )

    def test_two_entries_sharing_one_id_are_both_reported(self):
        """There used to be a dedicated cross-watcher duplicate check. Both
        entries being refused individually is strictly stronger, and this asserts
        the old hazard cannot reappear as "no issues at all"."""
        cfg, issues = collect_config(write_config("""\
            - {name: w1, rooms: {include: [general]}, session_id: same, agent: default}
            - {name: w2, rooms: {include: [dev]}, session_id: same, agent: default}
            """))
        self.assertEqual(len(issues), 2, [i.message for i in issues])
        self.assertEqual(cfg.watcher_rules, [])


if __name__ == "__main__":
    unittest.main()
