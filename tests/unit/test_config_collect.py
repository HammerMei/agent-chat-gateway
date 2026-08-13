"""Unit tests for gateway/config.py's collect_config() — the fault-tolerant
counterpart to GatewayConfig.from_file() (which stays strict/fail-fast,
unchanged, for its existing production callers). These pin the specific
correctness properties an independent code review verified/caught while
this was built: partial progress is preserved across a structural failure
elsewhere, and a failed multi-room watcher entry never leaks a phantom
"duplicate name" collision onto a later, genuinely valid entry.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import _parse_one_watcher_entry, collect_config


class _CollectConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)


class TestCollectConfigWatcherNameLeak(_CollectConfigTestBase):
    """PR review finding: seen_watcher_names (shared across ALL watcher
    entries in one collect_config() pass) used to be updated AS EACH ROOM
    was processed, not just once the whole entry succeeded. A multi-room
    entry that registered its first room's name fine and then raised on a
    LATER room left that first room's name permanently staged as "seen" —
    even though the entry's failure means NONE of its watchers actually
    exist in the result — so a later, perfectly valid entry wanting that
    same name was rejected as a false "duplicate"."""

    def test_a_failed_multi_room_entry_does_not_poison_later_valid_entries(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: rc-random
                connector: rc
                room: "collision-room"
              - connector: rc
                rooms: ["general", "random"]
              - name: rc-general
                connector: rc
                room: "another-room"
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        names = [w.name for w in config.watchers]
        # entry 2 ("random" room auto-name collides with entry 0's explicit
        # name "rc-random"? No — entry 1's SECOND room "random" auto-names
        # to "rc-random" too, genuinely colliding with entry 0 — entry 1 as
        # a whole is correctly rejected. What must NOT happen: entry 2
        # ("rc-general") getting rejected as a phantom duplicate of
        # something entry 1 never actually contributed.
        self.assertIn("rc-general", names)
        watcher_issues = [i for i in issues if i.entity_kind == "watcher"]
        # Exactly the genuinely-broken entry (index 1) should be reported —
        # not entry 2.
        self.assertEqual(len(watcher_issues), 1)


class TestCollectConfigPartialProgressPreserved(_CollectConfigTestBase):
    """PR review finding: several structural-failure branches used to
    `return None, issues` outright, discarding every connector/agent that
    had ALREADY parsed successfully — silently hiding an unrelated,
    already-real problem (e.g. a connector's empty credentials) behind a
    completely different structural issue elsewhere in the file."""

    def test_invalid_default_agent_still_returns_the_good_connectors(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              broken_default:
                type: claude
              other_agent:
                type: claude
                working_directory: {self.agent_dir}
            default_agent: broken_default
            watchers:
              - connector: rc1
                agent: other_agent
                room: general
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(config.watchers, [])  # can't safely expand without a valid default_agent
        self.assertTrue(
            any("default_agent" in i.message and i.entity_kind == "global" for i in issues)
        )

    def test_all_connectors_failing_still_returns_the_good_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.connectors, [])
        self.assertEqual(list(config.agents), ["default"])

    def test_zero_agents_still_returns_the_good_connectors(self):
        config_path = self._write("""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: "http://localhost:3000", username: "", password: ""}
            agents:
              broken:
                type: claude
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(config.agents, {})

    def test_malformed_watchers_block_still_returns_good_connectors_and_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers: {{not: a-list}}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(list(config.agents), ["default"])
        self.assertEqual(config.watchers, [])

    def test_malformed_watchers_block_still_keeps_a_valid_max_queue_depth_and_scheduler(self):
        """PR review finding: every structural early-return branch above
        used to hardcode max_queue_depth=100/scheduler=SchedulerConfig()
        instead of actually parsing them — silently discarding an
        otherwise-valid value behind a completely unrelated structural
        issue elsewhere in the file (the exact "don't hide an unrelated,
        already-successful value behind a different issue" bug this whole
        function exists to avoid for connectors/agents/watchers). These two
        fields have no entity dependency on watchers: at all."""
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers: {{not: a-list}}
            max_queue_depth: 42
            scheduler:
              completed_job_ttl_days: 30
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 42)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 30)


class TestCollectConfigNonStringScalarFields(_CollectConfigTestBase):
    """PR review finding (round 6): the same class of bug round 5 fixed for
    a non-string 'name'/'inherits' (a truthy-but-wrong-type raw value
    slipping past a bare `if not x` check into a hash-based `in`/`.get()`,
    or a string method) was also live on five other raw scalar reference
    fields — connector 'type', watcher 'connector'/'agent'/'room'/
    'session_id', and the top-level 'default_agent' — reachable via BOTH
    from_file() (see test_config_loading.py's
    TestConfigValidationHardening for the strict-path pins) and
    collect_config(). Each must surface as a collected, per-entity/global
    ConfigIssue — never an uncaught TypeError/AttributeError that would
    abort collect_config() (or crash gateway/config_validate.py's
    validate_config() a layer up) for the WHOLE file."""

    def test_non_string_connector_type_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: [rocketchat]
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'type' must be a string" in i.message for i in issues))

    def test_non_string_watcher_connector_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - room: general
                connector: [rc]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'connector' must be a string" in i.message for i in issues))

    def test_non_string_watcher_agent_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - room: general
                connector: rc
                agent: [default]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'agent' must be a string" in i.message for i in issues))

    def test_non_string_watcher_room_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - room: 12345
                connector: rc
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'room' must be a string" in i.message for i in issues))

    def test_non_string_watcher_session_id_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - room: general
                connector: rc
                session_id: [abc]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'session_id' must be a string" in i.message for i in issues))

    def test_non_string_default_agent_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            default_agent: [prod]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'default_agent' must be a string" in i.message for i in issues))


class TestCollectConfigNonStringNameHint(_CollectConfigTestBase):
    """PR review finding: a connector/watcher entry's own `name:` might
    itself be malformed (e.g. a list instead of a string) on an entry that
    ALSO fails for some unrelated reason — ConfigIssue.entity_name is typed
    `str | None` everywhere downstream, and EditableConfig's save-gate
    (model.py's _new_errors_introduced_by_this_save()) puts this value
    straight into a set of tuples, which raises an uncaught
    `TypeError: unhashable type` for anything non-hashable (a list).
    collect_config() must fall back to the same "(index i)" label an
    absent name already gets, rather than using the malformed value
    verbatim."""

    def test_non_string_connector_name_falls_back_to_index_label(self):
        config_path = self._write(f"""\
            connectors:
              - name: [a, b]
                type: rocketchat
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        connector_issues = [i for i in issues if i.entity_kind == "connector"]
        self.assertEqual(len(connector_issues), 1)
        self.assertEqual(connector_issues[0].entity_name, "(index 0)")
        # Never crashes building a set of these — the actual failure mode
        # PR review caught (EditableConfig._new_errors_introduced_by_this_save()).
        {(i.entity_kind, i.entity_name, i.message) for i in issues}

    def test_non_string_watcher_name_falls_back_to_index_label(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: [a, b]
                connector: rc
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        watcher_issues = [i for i in issues if i.entity_kind == "watcher"]
        self.assertEqual(len(watcher_issues), 1)
        self.assertEqual(watcher_issues[0].entity_name, "(index 0)")
        {(i.entity_kind, i.entity_name, i.message) for i in issues}


class TestParseOneWatcherEntryEmptyConnectors(_CollectConfigTestBase):
    """PR review finding: GatewayConfig.from_file() can never call
    _parse_one_watcher_entry() with an empty `connectors` list — an earlier
    structural check always raises first. collect_config() guards against
    it too (its own "no connectors parsed successfully" branch returns
    before ever reaching the watcher loop). But
    EditableConfig.expanded_watchers() calls this function directly, per
    raw watcher entry, against whatever partial `connectors` list
    collect_config() returned — so an all-connectors-failed config CAN
    legitimately reach this function with `connectors=[]`. Previously this
    crashed with an uncaught IndexError (`connectors[0].name`) instead of
    raising the ValueError every caller's `except ValueError` expects."""

    def test_no_explicit_connector_and_zero_connectors_raises_value_error_not_index_error(self):
        with self.assertRaises(ValueError):
            _parse_one_watcher_entry(
                {"name": "w1", "room": "general"},
                0,
                watcher_templates={},
                connector_names=set(),
                connectors=[],
                agents={},
                default_agent="",
                config_dir=Path(self.tmp),
                seen_watcher_names=set(),
            )


class TestCollectConfigQueueSchedulerSessionId(_CollectConfigTestBase):
    """PR review finding: collect_config()'s max_queue_depth/scheduler:/
    duplicate-session_id branches (unlike connectors/agents/watchers) had no
    dedicated test directly against collect_config() itself — only
    indirectly, via test_config_validate.py's lint regression test. Also
    pins that max_queue_depth and scheduler: are validated INDEPENDENTLY: a
    bad one falls back to its own default without discarding the other's
    genuinely valid value."""

    def _base_config(self, extra: str) -> str:
        # `extra` is interpolated into an already-dedented block below, so
        # it needs the SAME 12-space indentation as every other line here —
        # otherwise textwrap.dedent() (in _write()) sees an inconsistent
        # common prefix across lines and no-ops, breaking the YAML entirely.
        indented_extra = textwrap.indent(extra, " " * 12)
        return self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
{indented_extra}
        """)

    def test_invalid_max_queue_depth_falls_back_to_default_and_keeps_a_valid_scheduler(self):
        config_path = self._base_config(
            "max_queue_depth: -1\nscheduler: {completed_job_ttl_days: 30}"
        )
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 100)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 30)
        self.assertTrue(any("max_queue_depth" in i.message for i in issues))

    def test_invalid_scheduler_falls_back_to_default_and_keeps_a_valid_max_queue_depth(self):
        config_path = self._base_config("max_queue_depth: 42\nscheduler: not-a-mapping")
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 42)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 7)  # dataclass default
        self.assertTrue(any("scheduler" in i.message for i in issues))

    def test_duplicate_session_id_across_watchers_is_an_issue_not_a_discard(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
                session_id: sticky-1
              - name: w2
                connector: rc
                agent: default
                room: dev
                session_id: sticky-1
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([w.name for w in config.watchers], ["w1", "w2"])
        self.assertTrue(any("Duplicate sticky session_id" in i.message for i in issues))


class TestCollectConfigOnTheFlyWatcherFields(_CollectConfigTestBase):
    """exclude_room / room: "*" (WatcherConfig), and the TTL keys that moved off the
    agent — docs/design/dynamic-watcher-design.md. Same class of requirement as the
    fields above: a bad value must surface as a collected, per-entity ConfigIssue
    through collect_config(), never an uncaught exception that aborts the whole
    file."""

    def test_a_ttl_key_left_on_an_agent_is_a_collected_agent_issue(self):
        """These moved to the watcher rule (design §5.4), and a leftover key is a
        hard error rather than a silently ignored one — so it must arrive here as an
        attributed issue, not as an exception that stops the pass."""
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                session_idle_days: 30
            watchers:
              - room: general
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.agents, {})
        agent_issues = [i for i in issues if i.entity_kind == "agent"]
        self.assertEqual(len(agent_issues), 1, [i.message for i in issues])
        self.assertIn("moved to the watcher rule", agent_issues[0].message)

    def test_wildcard_room_is_a_collected_watcher_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - connector: rc
                agent: default
                room: "*"
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.watchers, [])
        self.assertTrue(any("not implemented yet" in i.message for i in issues))


if __name__ == "__main__":
    unittest.main()
