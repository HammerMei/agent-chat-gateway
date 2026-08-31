"""`default_agent:` is gone, and a rule must name the agent it runs on.

A rule's `agent:` used to be optional. It fell back to the top-level
`default_agent:`, which itself fell back to `next(iter(agents))` — whichever
agent happened to come first in the file. So a config could bind a room to a
backend, a working directory and a tool policy that nobody wrote down, and
reordering the `agents:` block silently rebound it.

That is the same objection `_resolve_watcher_connector`'s own docstring raises
about the connector fallback it guards: *"it binds the watcher to the wrong
account silently, and the canonical multi-agent setup gives every agent its own
account."* The saving offered in exchange was one line per rule.

**What replaces it is not "type it out every time".** `agent` is inheritable, so
one `watcher_templates:` entry still expresses "these rules all use this agent"
— the difference is that the sharing is written down and names its template,
instead of being a rule about document order. That is why "required" here means
required on the MERGED rule, and why the JSON schema does NOT list `agent` in
`watcherRule.required`: the schema sees the raw document, where a rule that
inherits legitimately omits the key.

Run with:
    uv run python -m pytest tests/unit/test_default_agent_removed.py -v
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import TOP_LEVEL_KEYS, GatewayConfig, collect_config
from gateway.config_validate import validate_config

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "gateway/schema/config.schema.json").read_text()
)

BASE = """\
    connectors:
      - name: rc
        type: rocketchat
        server: {url: http://localhost:3000, username: bot, password: pw}
    agents:
      alpha: {type: claude, working_directory: /tmp}
      beta: {type: claude, working_directory: /tmp}
"""


def write(body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent(BASE) + textwrap.dedent(body))
        return f.name


class TestTheTopLevelKeyIsGone(unittest.TestCase):
    def test_it_is_not_a_valid_top_level_key(self):
        self.assertNotIn("default_agent", TOP_LEVEL_KEYS)

    def test_an_old_config_says_so_by_name(self):
        """Reported by the general unknown-key rule, like `watchers:` before it —
        no dedicated message to maintain, and the valid keys are listed."""
        path = write("""\
            default_agent: alpha
            watcher_rules:
              - {name: r1, agent: alpha, rooms: {include: [general]}}
        """)
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(path)
        msg = str(cm.exception)
        self.assertIn("'default_agent'", msg)
        self.assertIn("does not use", msg)

    def test_the_config_object_no_longer_carries_it(self):
        path = write("""\
            watcher_rules:
              - {name: r1, agent: alpha, rooms: {include: [general]}}
        """)
        config = GatewayConfig.from_file(path)
        self.assertFalse(hasattr(config, "default_agent"))
        self.assertFalse(hasattr(config, "agent"), "the convenience property went with it")

    def test_the_schema_rejects_it_too(self):
        self.assertNotIn("default_agent", SCHEMA["properties"])


class TestARuleMustNameItsAgent(unittest.TestCase):
    def test_omitting_it_is_a_load_error(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write("""\
                watcher_rules:
                  - {name: r1, rooms: {include: [general]}}
            """))
        msg = str(cm.exception)
        self.assertIn("'agent' is required", msg)

    def test_the_error_lists_the_agents_available(self):
        """So the fix is in the message, not in another file."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write("""\
                watcher_rules:
                  - {name: r1, rooms: {include: [general]}}
            """))
        msg = str(cm.exception)
        self.assertIn("alpha", msg)
        self.assertIn("beta", msg)

    def test_the_error_points_at_the_template_route(self):
        """The replacement for the fallback, named where someone hits the wall."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write("""\
                watcher_rules:
                  - {name: r1, rooms: {include: [general]}}
            """))
        self.assertIn("watcher template", str(cm.exception))

    def test_an_explicit_null_does_not_count_as_naming_one(self):
        """`agent: null` is how an entry declines an inherited value — declining
        the only source leaves the rule with none, which is the same error rather
        than a silent fallback."""
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(write("""\
                watcher_templates:
                  shared: {agent: alpha}
                watcher_rules:
                  - {name: r1, inherits: shared, agent: null, rooms: {include: [general]}}
            """))
        self.assertIn("'agent' is required", str(cm.exception))

    def test_no_agent_is_picked_by_document_order(self):
        """The actual defect: two agents, and nothing silently chooses the first."""
        with self.assertRaises(ValueError):
            GatewayConfig.from_file(write("""\
                watcher_rules:
                  - {name: r1, rooms: {include: [general]}}
            """))


class TestATemplateStillSharesOne(unittest.TestCase):
    """The capability that replaced the fallback — and the reason removing it
    costs a config with many rules nothing."""

    def test_a_rule_inherits_its_agent(self):
        config = GatewayConfig.from_file(write("""\
            watcher_templates:
              shared: {agent: beta}
            watcher_rules:
              - {name: r1, inherits: shared, rooms: {include: [eng-*]}}
              - {name: r2, inherits: shared, rooms: {include: [ops-*]}}
        """))
        self.assertEqual([r.agent for r in config.watcher_rules], ["beta", "beta"])

    def test_a_rule_can_still_override_the_shared_one(self):
        config = GatewayConfig.from_file(write("""\
            watcher_templates:
              shared: {agent: beta}
            watcher_rules:
              - {name: r1, inherits: shared, agent: alpha, rooms: {include: [eng-*]}}
        """))
        self.assertEqual(config.watcher_rules[0].agent, "alpha")

    def test_the_schema_does_not_demand_the_key_on_the_raw_entry(self):
        """`agent` is required on the MERGED rule, which a document-shape schema
        cannot express — listing it in `required` would reject the inheriting
        rule above. Same reason `rooms` is not listed."""
        self.assertEqual(SCHEMA["$defs"]["watcherRule"]["required"], ["name"])

    def test_a_template_supplied_agent_validates_clean(self):
        path = write("""\
            watcher_templates:
              shared: {agent: beta}
            watcher_rules:
              - {name: r1, inherits: shared, rooms: {include: [eng-*]}}
        """)
        result = validate_config(path)
        self.assertTrue(result.ok, result.errors)


class TestThroughTheFaultTolerantLoader(unittest.TestCase):
    def test_the_missing_agent_is_attributed_and_others_keep_parsing(self):
        config, issues = collect_config(write("""\
            watcher_rules:
              - {name: broken, rooms: {include: [general]}}
              - {name: fine, agent: alpha, rooms: {include: [dev]}}
        """))
        self.assertEqual([(i.entity_kind, i.entity_name) for i in issues],
                         [("watcher", "broken")])
        self.assertEqual([r.name for r in config.watcher_rules], ["fine"])

    def test_validate_config_reports_it_as_an_error(self):
        result = validate_config(write("""\
            watcher_rules:
              - {name: r1, rooms: {include: [general]}}
        """))
        self.assertFalse(result.ok)
        self.assertTrue(any("'agent' is required" in e for e in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
