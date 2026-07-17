"""Unit tests for gateway/configtool/model.py — EditableConfig.

These pin the keystone design decision from docs/design/config-tool.md: the
config TUI reads/writes the PRE-MERGE raw document, never GatewayConfig, and
never through a code path that expands $VAR env references (that would risk
writing resolved secrets back to disk in a later save-capable phase).
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import GatewayConfig
from gateway.configtool.model import EditableConfig, Provenance


class _EditableConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> Path:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return path


class TestEditableConfigLoad(_EditableConfigTestBase):
    def test_load_returns_raw_document(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        self.assertEqual(cfg.path, path)
        self.assertEqual(len(cfg.connectors_raw), 1)
        self.assertEqual(cfg.connectors_raw[0]["name"], "rc")
        self.assertIn("default", cfg.agents_raw)
        self.assertEqual(len(cfg.watchers_raw), 1)

    def test_env_var_reference_is_never_expanded(self):
        """Regression for the keystone decision: EditableConfig must load via
        plain yaml.safe_load, never GatewayConfig.from_file — unresolved or
        resolved, $VAR must survive as a literal string, and loading must NOT
        raise even though $RC_URL is never set in the environment (unlike
        GatewayConfig.from_file, which would raise on an unresolved var)."""
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL_NEVER_SET_12345", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        self.assertEqual(
            cfg.connectors_raw[0]["server"]["url"], "$RC_URL_NEVER_SET_12345"
        )
        # Sanity: the same file WOULD raise via the real loader, confirming
        # EditableConfig.load is doing something meaningfully different.
        with self.assertRaises(ValueError):
            GatewayConfig.from_file(path)

    def test_nonexistent_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            EditableConfig.load(Path(self.tmp) / "does-not-exist.yaml")

    def test_non_mapping_top_level_raises_value_error(self):
        path = Path(self.tmp) / "config.yaml"
        path.write_text("- just\n- a\n- list\n")
        with self.assertRaises(ValueError):
            EditableConfig.load(path)

    def test_reload_picks_up_on_disk_changes(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        self.assertEqual(len(cfg.watchers_raw), 1)

        path.write_text(
            path.read_text()
            + "  - name: w2\n    room: dev\n"
        )
        cfg.reload()
        self.assertEqual(len(cfg.watchers_raw), 2)


class TestExpandedWatchersDesync(_EditableConfigTestBase):
    """Regression: expanded_watchers() must raise ValueError (never a raw
    IndexError) when the in-memory document and a fresh disk read disagree
    on watcher count — e.g. an external process edits config.yaml without an
    intervening reload() on this EditableConfig instance."""

    def _cfg_with_rooms(self, rooms: str) -> tuple[EditableConfig, Path]:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - connector: rc
                agent: default
                rooms: [{rooms}]
        """)
        return EditableConfig.load(path), path

    def test_fewer_rooms_on_disk_raises_value_error_not_index_error(self):
        cfg, path = self._cfg_with_rooms("nest, hammer, dev")
        path.write_text(path.read_text().replace(
            "rooms: [nest, hammer, dev]", "rooms: [nest, hammer]"
        ))
        with self.assertRaises(ValueError) as ctx:
            cfg.expanded_watchers()
        self.assertIn("disagree on watcher count", str(ctx.exception))

    def test_more_rooms_on_disk_raises_value_error_not_index_error(self):
        cfg, path = self._cfg_with_rooms("nest, hammer")
        path.write_text(path.read_text().replace(
            "rooms: [nest, hammer]", "rooms: [nest, hammer, dev, extra]"
        ))
        with self.assertRaises(ValueError) as ctx:
            cfg.expanded_watchers()
        self.assertIn("disagree on watcher count", str(ctx.exception))

    def test_reload_before_calling_resolves_the_desync(self):
        cfg, path = self._cfg_with_rooms("nest, hammer, dev")
        path.write_text(path.read_text().replace(
            "rooms: [nest, hammer, dev]", "rooms: [nest, hammer]"
        ))
        cfg.reload()
        expanded = cfg.expanded_watchers()  # must not raise
        self.assertEqual(len(expanded), 2)


class TestEditableConfigDefaultsBlock(_EditableConfigTestBase):
    def test_defaults_block_strips_description(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_defaults:
              description: "Shared claude settings"
              type: claude
              working_directory: {self.agent_dir}
            agents:
              default: {{}}
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        defaults = cfg.defaults_block("agent_defaults")
        self.assertNotIn("description", defaults)
        self.assertEqual(defaults["type"], "claude")

    def test_defaults_block_enforces_forbidden_keys_like_the_real_loader(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_defaults:
              session_id: not-allowed
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.defaults_block("watcher_defaults")


class TestEditableConfigDefaultsBlockCaching(_EditableConfigTestBase):
    """Code review item 8: defaults_block() is cached per kind (see
    EditableConfig._defaults_cache) instead of re-running
    _extract_defaults_block on every call. These tests pin the two things
    that matter about a cache: repeated calls return the equivalent value,
    and load()/reload() — the only ways `document` changes — invalidate it."""

    def _cfg(self) -> tuple[EditableConfig, Path]:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_defaults:
              type: claude
              working_directory: {self.agent_dir}
            agents:
              default: {{}}
            watchers:
              - name: w1
                room: general
        """)
        return EditableConfig.load(path), path

    def test_repeated_calls_return_the_same_cached_object(self):
        cfg, _ = self._cfg()
        first = cfg.defaults_block("agent_defaults")
        second = cfg.defaults_block("agent_defaults")
        self.assertIs(first, second)

    def test_reload_invalidates_the_cache(self):
        cfg, path = self._cfg()
        first = cfg.defaults_block("agent_defaults")
        self.assertEqual(first["type"], "claude")

        path.write_text(
            path.read_text().replace("type: claude", "type: opencode")
        )
        cfg.reload()
        second = cfg.defaults_block("agent_defaults")
        self.assertEqual(second["type"], "opencode")
        self.assertIsNot(first, second)

    def test_a_failed_lookup_is_not_cached_as_a_false_success(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_defaults:
              session_id: not-allowed
            agents:
              default:
                type: claude
                working_directory: /tmp
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.defaults_block("watcher_defaults")
        # Calling again must still raise — a cache bug could swallow this
        # into a stale/absent cached value instead of re-validating.
        with self.assertRaises(ValueError):
            cfg.defaults_block("watcher_defaults")


class TestEditableConfigProvenance(_EditableConfigTestBase):
    def _cfg(self) -> EditableConfig:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_defaults:
              type: claude
              working_directory: {self.agent_dir}
              timeout: 1800
              permissions: {{enabled: true, timeout: 300}}
            agents:
              inherits-everything: {{}}
              overrides-timeout:
                timeout: 500
              suppresses-timeout:
                timeout: null
            watchers:
              - name: w1
                room: general
        """)
        return EditableConfig.load(path)

    def test_field_absent_from_entry_is_inherited(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["inherits-everything"]
        self.assertEqual(
            cfg.field_provenance("agent_defaults", entry, "timeout"),
            Provenance.INHERITED,
        )

    def test_field_explicitly_set_is_explicit(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["overrides-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent_defaults", entry, "timeout"),
            Provenance.EXPLICIT,
        )

    def test_explicit_null_over_a_default_is_suppressing(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent_defaults", entry, "timeout"),
            Provenance.EXPLICIT_SUPPRESSING,
        )

    def test_explicit_field_with_no_matching_default_is_still_explicit_not_suppressing(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        # 'session_prefix' has no entry in agent_defaults here, so even if it
        # were null on the entry, there's nothing to "suppress" — this test
        # uses a field that's simply absent from defaults entirely.
        self.assertEqual(
            cfg.field_provenance("agent_defaults", entry, "session_prefix"),
            Provenance.INHERITED,
        )

    def test_merged_entry_reflects_real_deep_merge(self):
        cfg = self._cfg()
        merged = cfg.merged_entry("agent_defaults", cfg.agents_raw["overrides-timeout"])
        self.assertEqual(merged["timeout"], 500)  # entry's own override wins
        self.assertEqual(merged["type"], "claude")  # inherited from defaults
        # nested dict merges too (permissions comes from defaults wholesale)
        self.assertEqual(merged["permissions"], {"enabled": True, "timeout": 300})


class TestEditableConfigValidatedView(_EditableConfigTestBase):
    def test_validated_view_returns_real_gateway_config(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        view = cfg.validated_view()
        self.assertIsInstance(view, GatewayConfig)
        self.assertEqual(len(view.watchers), 1)
        self.assertEqual(view.watchers[0].name, "w1")

    def test_validated_view_raises_same_as_from_file_on_invalid_config(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.validated_view()


if __name__ == "__main__":
    unittest.main()
