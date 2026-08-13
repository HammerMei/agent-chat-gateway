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

This PR is the config half. `WatcherConfig.session_id` still exists as a field that
can now only ever be `None`, which makes the runtime's pinned-session branches dead
code; removing those is the second half, and separating them keeps that removal
provably behaviour-neutral.

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
            GatewayConfig.from_file(write_config('- {room: general, session_id: "ses_abc123"}\n'))
        msg = str(cm.exception)
        self.assertIn("'session_id' is no longer supported", msg)

    def test_the_error_names_the_handoff_replacement(self):
        """There is no replacement *field*, so the error has to describe the
        replacement *mechanism* or it reads as an arbitrary removal."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- {room: general, session_id: "x"}\n'))
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
                        write_config(f"- {{room: general, session_id: {value}}}\n")
                    )
                self.assertIn("no longer supported", str(cm.exception))

    def test_an_unrelated_unknown_key_is_still_ignored(self):
        """The contrast that makes the check necessary: unknown keys are dropped
        silently by design, so `session_id` needed an explicit refusal rather than
        deletion. If this ever starts raising, the refusal above is redundant."""
        cfg = GatewayConfig.from_file(
            write_config("- {room: general, some_future_key: 7}\n")
        )
        self.assertEqual([w.room for w in cfg.watchers], ["general"])

    def test_a_config_without_it_is_unaffected(self):
        cfg = GatewayConfig.from_file(write_config("- {room: general, name: w1}\n"))
        self.assertEqual([w.name for w in cfg.watchers], ["w1"])
        self.assertIsNone(cfg.watchers[0].session_id)


class TestItCannotArriveByInheritance(unittest.TestCase):
    """A template is merged into the entry before the entry is parsed, so a template
    is the one place a removed key could still slip through."""

    def test_a_template_setting_it_is_refused_and_the_template_is_named(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {room: general, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("watcher_templates['standard']", msg)
        self.assertIn("session_id", msg)

    def test_the_template_error_does_not_tell_you_to_move_it_per_entry(self):
        """`session_id` stays in TEMPLATE_FORBIDDEN_KEYS so the template is named
        rather than every entry inheriting it — but the generic wording there ("set it
        per-entry") is *wrong* for a field nothing accepts, and a correct attribution
        carrying a wrong instruction is worse than no detail. So the removed keys are
        described separately."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {room: general, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("does not exist at all any more", msg)
        self.assertNotIn("must be set per-entry", msg)

    def test_an_identity_key_still_gets_the_per_entry_wording(self):
        """The other keys in that set are not removed, so their advice must survive."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {room: general, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    name: shared\n",
            ))
        msg = str(cm.exception)
        self.assertIn("must be set per-entry", msg)
        self.assertNotIn("does not exist at all", msg)

    def test_a_template_setting_both_kinds_explains_each(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config(
                "- {room: general, inherits: standard}\n",
                extra="watcher_templates:\n  standard:\n    name: shared\n    session_id: sticky\n",
            ))
        msg = str(cm.exception)
        self.assertIn("must be set per-entry", msg)
        self.assertIn("does not exist at all any more", msg)


class TestTheRoomCountNoLongerEntersIntoIt(unittest.TestCase):
    def test_it_is_refused_on_a_single_room_entry_too(self):
        """The old rule was "only settable with exactly one room", so a single-room
        entry was the *legal* case. It is now refused like any other."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- {room: general, session_id: "x"}\n'))
        self.assertIn("no longer supported", str(cm.exception))

    def test_and_on_a_multi_room_entry(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config('- {rooms: [a, b], session_id: "x"}\n'))
        self.assertIn("no longer supported", str(cm.exception))

    def test_name_keeps_its_single_room_restriction(self):
        """`name` was governed by the same sentence in the docs; only `session_id` is
        removed, so `name`'s rule must be intact rather than removed alongside it."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write_config("- {rooms: [a, b], name: shared}\n"))
        self.assertIn("'name' can only be set when there is exactly one room", str(cm.exception))


class TestThroughTheFaultTolerantLoader(unittest.TestCase):
    def test_it_is_attributed_and_later_entries_still_parse(self):
        cfg, issues = collect_config(write_config("""\
            - {name: pinned, room: general, session_id: "ses_abc"}
            - {name: fine, room: dev}
            """))
        self.assertEqual([(i.entity_kind, i.entity_name) for i in issues],
                         [("watcher", "pinned")])
        self.assertEqual([w.name for w in cfg.watchers], ["fine"])

    def test_validate_config_reports_it_as_an_error(self):
        result = validate_config(write_config('- {room: general, session_id: "x"}\n'))
        self.assertFalse(result.ok)
        self.assertTrue(
            any("no longer supported" in e for e in result.errors), result.errors
        )

    def test_the_duplicate_pass_is_gone_rather_than_silently_passing(self):
        """Two watchers sharing one id used to be a dedicated cross-watcher check.
        Both entries are now refused individually, which is strictly stronger — and
        this asserts the old hazard cannot reappear as "no issues at all"."""
        cfg, issues = collect_config(write_config("""\
            - {name: w1, room: general, session_id: same}
            - {name: w2, room: dev, session_id: same}
            """))
        self.assertEqual(len(issues), 2, [i.message for i in issues])
        self.assertEqual(cfg.watchers, [])


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
