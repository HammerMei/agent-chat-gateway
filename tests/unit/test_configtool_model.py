"""Unit tests for gateway/configtool/model.py — EditableConfig.

These pin the keystone design decision from docs/design/config-tool.md: the
config TUI reads/writes the PRE-MERGE raw document, never GatewayConfig, and
never through a code path that expands $VAR env references (that would risk
writing resolved secrets back to disk in a later save-capable phase).
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from gateway.config import GatewayConfig
from gateway.config_validate import validate_config
from gateway.configtool.model import EditableConfig, Provenance, StatusIndex


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
        plain yaml.safe_load, never GatewayConfig.from_file. docs/design/
        config-tool.md decision 6, final revision: GatewayConfig.from_file()
        itself no longer expands $VAR either (a value that looks like a
        placeholder is a plain literal, same as everywhere else) — so this
        now also holds true via the real loader, confirmed below as a
        cross-check that EditableConfig.load() didn't accidentally start
        relying on that no-longer-distinctive behavior."""
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
        # Cross-check: the real loader treats it identically now (a plain
        # literal, not raised on, not resolved) — EditableConfig.load()'s
        # own reasons for using plain yaml.safe_load (provenance, raw
        # rooms: groupings — see module docstring) still stand regardless.
        real_cfg = GatewayConfig.from_file(path)
        self.assertEqual(
            real_cfg.connectors[0].raw["server"]["url"], "$RC_URL_NEVER_SET_12345"
        )

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


class TestExpandedWatchersMalformedWatcherTemplates(_EditableConfigTestBase):
    """PR review finding: expanded_watchers() re-parses `watcher_templates:`
    straight off disk, independently of collect_config() (which already
    tolerates this same failure internally, falling back to watchers=[]
    plus its own ConfigIssue). Without an equivalent fallback here, a
    malformed `watcher_templates:` block used to make this method's
    ValueError propagate all the way up through OverviewScreen's
    `except (ValueError, FileNotFoundError): expanded = None`, collapsing
    the ENTIRE watchers table to the "(unavailable)" placeholder — the
    exact all-or-nothing failure mode this whole method exists to avoid —
    even though every watcher entry itself was perfectly fine."""

    def test_malformed_watcher_templates_block_does_not_take_down_every_watcher(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_templates: not-a-mapping
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
        """)
        cfg = EditableConfig.load(path)
        expanded = cfg.expanded_watchers()  # must not raise
        self.assertEqual([ew.watcher.name for ew in expanded], ["w1"])

    def test_a_watcher_that_actually_needs_the_broken_template_still_drops_its_own_row(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_templates: not-a-mapping
            watchers:
              - name: w1
                connector: rc
                agent: default
                room: general
              - name: w2
                connector: rc
                agent: default
                room: dev
                inherits: standard
        """)
        cfg = EditableConfig.load(path)
        expanded = cfg.expanded_watchers()  # must not raise
        # w2 references a template that can't be resolved at all (the whole
        # block is malformed) — its own row drops, same as any other
        # independent per-entry failure; w1 (unaffected) still displays.
        self.assertEqual([ew.watcher.name for ew in expanded], ["w1"])


class TestEditableConfigTemplates(_EditableConfigTestBase):
    def test_templates_strips_description(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                description: "Shared claude settings"
                type: claude
                working_directory: {self.agent_dir}
            agents:
              default:
                inherits: standard
            watchers:
              - name: w1
                room: general
        """)
        cfg = EditableConfig.load(path)
        standard = cfg.templates("agent")["standard"]
        self.assertNotIn("description", standard)
        self.assertEqual(standard["type"], "claude")

    def test_templates_enforces_forbidden_keys_like_the_real_loader(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_templates:
              standard:
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
            cfg.templates("watcher")


class TestEditableConfigTemplatesCaching(_EditableConfigTestBase):
    """Code review item 8: templates() is cached per kind (see
    EditableConfig._templates_cache) instead of re-running
    _parse_templates_block on every call. These tests pin the two things
    that matter about a cache: repeated calls return the equivalent value,
    and load()/reload() — the only ways `document` changes — invalidate it."""

    def _cfg(self) -> tuple[EditableConfig, Path]:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
            agents:
              default:
                inherits: standard
            watchers:
              - name: w1
                room: general
        """)
        return EditableConfig.load(path), path

    def test_repeated_calls_return_the_same_cached_object(self):
        cfg, _ = self._cfg()
        first = cfg.templates("agent")
        second = cfg.templates("agent")
        self.assertIs(first, second)

    def test_reload_invalidates_the_cache(self):
        cfg, path = self._cfg()
        first = cfg.templates("agent")
        self.assertEqual(first["standard"]["type"], "claude")

        path.write_text(
            path.read_text().replace("type: claude", "type: opencode")
        )
        cfg.reload()
        second = cfg.templates("agent")
        self.assertEqual(second["standard"]["type"], "opencode")
        self.assertIsNot(first, second)

    def test_a_failed_lookup_is_not_cached_as_a_false_success(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_templates:
              standard:
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
            cfg.templates("watcher")
        # Calling again must still raise — a cache bug could swallow this
        # into a stale/absent cached value instead of re-validating.
        with self.assertRaises(ValueError):
            cfg.templates("watcher")


class TestEditableConfigProvenance(_EditableConfigTestBase):
    def _cfg(self) -> EditableConfig:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 1800
                permissions: {{enabled: true, timeout: 300}}
            agents:
              inherits-everything:
                inherits: standard
              overrides-timeout:
                inherits: standard
                timeout: 500
              suppresses-timeout:
                inherits: standard
                timeout: null
              no-template-at-all:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        return EditableConfig.load(path)

    def test_field_absent_from_entry_is_inherited(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["inherits-everything"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.INHERITED,
        )

    def test_field_explicitly_set_is_explicit(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["overrides-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.EXPLICIT,
        )

    def test_explicit_null_over_a_template_value_is_suppressing(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.EXPLICIT_SUPPRESSING,
        )

    def test_field_absent_and_template_doesnt_set_it_is_default(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        # 'session_prefix' has no entry in the 'standard' template here, so
        # even though this entry DOES have inherits: standard, that template
        # doesn't cover this field — falls through to the code-level
        # dataclass default. Distinct from genuinely inheriting a value
        # (Provenance.DEFAULT, not INHERITED).
        self.assertEqual(
            cfg.field_provenance("agent", entry, "session_prefix"),
            Provenance.DEFAULT,
        )

    def test_field_absent_with_no_inherits_at_all_is_also_default(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["no-template-at-all"]
        # No inherits: key at all — the OTHER way to land on
        # Provenance.DEFAULT, collapsed into the same enum value as the case
        # above (see Provenance's own docstring in model.py).
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.DEFAULT,
        )

    def test_merged_entry_reflects_real_resolve_inherits(self):
        cfg = self._cfg()
        merged = cfg.merged_entry("agent", cfg.agents_raw["overrides-timeout"])
        self.assertEqual(merged["timeout"], 500)  # entry's own override wins
        self.assertEqual(merged["type"], "claude")  # inherited from the template
        # nested dict merges too (permissions comes from the template wholesale)
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


class TestEditableConfigDirtyTracking(_EditableConfigTestBase):
    def _cfg(self) -> EditableConfig:
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
        return EditableConfig.load(path)

    def test_freshly_loaded_config_is_not_dirty(self):
        cfg = self._cfg()
        self.assertFalse(cfg.dirty)

    def test_mark_dirty_sets_the_flag_and_clears_the_templates_cache(self):
        cfg = self._cfg()
        cached = cfg.templates("agent")  # populate the cache (empty here)
        cfg.document["agent_templates"] = {"standard": {"type": "opencode"}}
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        self.assertIsNot(cfg.templates("agent"), cached)
        self.assertEqual(cfg.templates("agent")["standard"]["type"], "opencode")

    def test_reload_clears_dirty(self):
        cfg = self._cfg()
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        cfg.reload()
        self.assertFalse(cfg.dirty)


class TestEditableConfigSave(_EditableConfigTestBase):
    """EditableConfig.save() — docs/design/config-tool.md decision 5:
    validate-before-write via a same-directory temp file, backup, atomic
    rename. The $VAR-survives-save test is the security keystone (advisor
    flagged this explicitly): if save() ever wrote a RESOLVED secret back to
    config.yaml, that's an incident, not a bug.
    """

    # A secret field (password), not url: config_validate's URL-format check
    # (added alongside the "does this look like a URL" validation) would
    # otherwise reject this placeholder as a malformed server.url.
    ENV_VAR_NAME = "RC_PASSWORD_FOR_CONFIGTOOL_SAVE_TEST"

    def setUp(self):
        super().setUp()
        os.environ[self.ENV_VAR_NAME] = "hunter2"
        self.addCleanup(os.environ.pop, self.ENV_VAR_NAME, None)

    def _valid_cfg_text(self) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: "${self.ENV_VAR_NAME}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """

    def test_save_preserves_the_unresolved_env_var_placeholder(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        # Read back with plain yaml (never the env-expanding loader) — the
        # literal "$VAR" string must survive, not the resolved secret.
        raw = yaml.safe_load(path.read_text())
        self.assertEqual(
            raw["connectors"][0]["server"]["password"], f"${self.ENV_VAR_NAME}"
        )

    def test_save_writes_a_timestamped_backup_of_the_prior_contents(self):
        path = self._write(self._valid_cfg_text())
        original_text = path.read_text()
        cfg = EditableConfig.load(path)
        cfg.save()
        backup_dir = path.parent / ".config-backups"
        backups = list(backup_dir.glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original_text)
        # secrets-adjacent files: backup dir owner-only, backup file owner-only
        self.assertEqual(oct(backup_dir.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(backups[0].stat().st_mode)[-3:], "600")

    def test_save_chmods_config_yaml_to_owner_only(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")

    def test_save_clears_the_dirty_flag(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        cfg.save()
        self.assertFalse(cfg.dirty)

    def test_save_leaves_no_leftover_temp_file(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        self.assertFalse((path.parent / "config.yaml.tmp").exists())

    def test_save_refuses_an_invalid_document_and_leaves_disk_untouched(self):
        path = self._write(self._valid_cfg_text())
        original_text = path.read_text()
        cfg = EditableConfig.load(path)
        del cfg.document["agents"]["default"]["working_directory"]
        cfg.mark_dirty()

        with self.assertRaises(ValueError):
            cfg.save()

        # The real file must be byte-identical to before the failed save —
        # no partial/temp/backup artifacts left behind either.
        self.assertEqual(path.read_text(), original_text)
        self.assertFalse((path.parent / "config.yaml.tmp").exists())
        self.assertFalse((path.parent / ".config-backups").exists())
        # dirty must stay set: the in-memory edit was never actually saved.
        self.assertTrue(cfg.dirty)

    def test_save_raises_file_not_found_if_the_config_file_is_gone(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            cfg.save()


class TestEditableConfigScopedSaveGate(_EditableConfigTestBase):
    """User-reported: the previous all-or-nothing gate meant a config with
    TWO independently-broken connectors could never be fixed (or even
    deleted) through the TUI — saving a fix to connector1 was rejected
    because connector2 was still broken, and vice versa. save() now only
    blocks on a genuinely NEW problem (gateway/config.py's collect_config(),
    which — unlike a single caught GatewayConfig.from_file() exception —
    surfaces every independently-broken connector/agent/watcher, not just
    whichever one from_file() would have hit first)."""

    def _two_broken_connectors_text(self) -> str:
        return f"""\
            connectors:
              - name: conn1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
              - name: conn2
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - connector: conn1
                agent: default
                room: general
              - connector: conn2
                agent: default
                room: dev
        """

    def test_fixing_one_connector_saves_despite_the_others_pre_existing_error(self):
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        conn1 = next(c for c in cfg.document["connectors"] if c["name"] == "conn1")
        conn1["server"]["username"] = "bot1"
        conn1["server"]["password"] = "pw1"
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        fixed = next(c for c in raw["connectors"] if c["name"] == "conn1")
        still_broken = next(c for c in raw["connectors"] if c["name"] == "conn2")
        self.assertEqual(fixed["server"]["username"], "bot1")
        # conn2's pre-existing, untouched problem survives exactly as it was.
        self.assertEqual(still_broken["server"]["username"], "")
        self.assertEqual(still_broken["server"]["password"], "")

    def test_deleting_the_fixed_connector_succeeds_despite_the_others_error(self):
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        cfg.document["connectors"] = [
            c for c in cfg.document["connectors"] if c["name"] != "conn1"
        ]
        cfg.document["watchers"] = [
            w for w in cfg.document["watchers"] if w["connector"] != "conn1"
        ]
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        names = {c["name"] for c in raw["connectors"]}
        self.assertEqual(names, {"conn2"})

    def test_a_genuinely_new_error_is_still_blocked_even_on_an_already_broken_config(self):
        """The scoped gate narrows what's IGNORED — it must not become a
        blanket pass. Introducing a brand-new problem (here: breaking the
        agent) while conn2 is already broken must still be refused."""
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        del cfg.document["agents"]["default"]["working_directory"]
        cfg.mark_dirty()

        with self.assertRaises(ValueError) as ctx:
            cfg.save()
        self.assertIn("working_directory is required", str(ctx.exception))

        # Disk must be untouched — same guarantee the existing
        # "refuses an invalid document" test already pins.
        raw = yaml.safe_load(path.read_text())
        self.assertIn("working_directory", raw["agents"]["default"])

    def test_fixing_an_unrelated_agent_does_not_reveal_a_hidden_connector_error(self):
        """PR review finding, chained from a bug in collect_config() itself:
        an invalid `default_agent` used to make collect_config() discard
        EVERY already-parsed connector/agent — so `before`'s findings never
        included a completely unrelated connector's own, already-real empty-
        credentials problem. Fixing the (unrelated) default_agent problem
        made that connector problem appear for the FIRST time in `after`,
        misclassifying an old, silent problem as "new" and blocking a
        perfectly good, unrelated fix."""
        path = self._write(f"""\
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
        cfg = EditableConfig.load(path)
        cfg.document["agents"]["broken_default"]["working_directory"] = str(self.agent_dir)
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        # rc1's pre-existing (untouched, unrelated) empty credentials survive
        # exactly as they were — this save neither fixed nor was blocked by them.
        self.assertEqual(raw["connectors"][0]["server"]["username"], "")

    def test_save_does_not_crash_when_a_broken_entry_has_a_non_string_name(self):
        """PR review finding: a watcher's own `name:` might itself be
        malformed (e.g. a list) on an entry that ALSO fails for an unrelated
        reason (here: no `room`/`rooms`) — gateway/config.py's
        collect_config() used to pass that malformed value straight through
        as ConfigIssue.entity_name, which _new_errors_introduced_by_this_save()
        above then puts into a set of tuples, raising an uncaught
        `TypeError: unhashable type: 'list'` instead of the clean ValueError
        every caller expects."""
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
              - name: rc2
                type: rocketchat
                server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: [a, b]
                connector: rc
        """)
        cfg = EditableConfig.load(path)
        # An entirely unrelated, otherwise-harmless edit — must not trip
        # over the pre-existing malformed watcher name while diffing
        # before/after findings.
        cfg.document["connectors"][1]["server"]["username"] = "bot2-renamed"
        cfg.mark_dirty()

        cfg.save()  # must not raise TypeError

        raw = yaml.safe_load(path.read_text())
        assert raw["connectors"][1]["server"]["username"] == "bot2-renamed"


class TestStatusIndexNonStringEntityName(_EditableConfigTestBase):
    """PR review finding: StatusIndex.__init__() groups findings by
    `(entity_kind, entity_name)` — a dict key that must be hashable.
    gateway/config_validate.py's _lint_config() used to attach a
    truthy-but-non-string 'name'/'inherits' value (e.g. a YAML list)
    straight onto a Finding.entity_name unchecked, which crashed HERE with
    an uncaught TypeError: unhashable type the first time the config TUI
    tried to build a StatusIndex from --lint findings — not at
    validate_config() itself (dataclass construction doesn't type-check),
    which is why this needs its own test beyond test_config_validate.py's
    Finding-shape assertions."""

    def test_a_non_string_connector_name_does_not_crash_status_index(self):
        path = self._write(f"""\
            connectors:
              - name: [a, b]
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
                reply_in_thread: false
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        result = validate_config(str(path), lint=True)
        StatusIndex(result.findings)  # must not raise TypeError

    def test_a_non_string_watcher_name_does_not_crash_status_index(self):
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
              - name: [a, b]
                connector: rc
                room: general
                session_id: null
        """)
        result = validate_config(str(path), lint=True)
        StatusIndex(result.findings)  # must not raise TypeError


class TestWatcherCrudPrimitives(_EditableConfigTestBase):
    """Config TUI Phase 3 (watcher CRUD): EditableConfig.add_watcher_rooms()/
    find_mergeable_watcher_entry()/remove_watcher_room() — the only two
    mutation primitives everything else (create, split-on-edit, room
    rename/move, delete) composes from. See docs/design/config-tool.md's
    Phase 3 section for the full merge/split design."""

    def _base_config(self, watchers_yaml: str = "") -> Path:
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
{watchers_yaml}
        """)

    def test_add_watcher_rooms_creates_a_new_entry_when_nothing_matches(self):
        path = self._base_config()
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["general"], {})
        self.assertEqual(added, ["general"])
        self.assertEqual(
            cfg.document["watchers"],
            [{"connector": "rc", "agent": "default", "room": "general"}],
        )
        self.assertTrue(cfg.dirty)

    def test_add_watcher_rooms_merges_multiple_rooms_into_one_group(self):
        """Creating 3 rooms in a single call (the new-watcher form's
        comma-separated room field) must produce ONE rooms: group, not 3
        separate near-duplicate entries."""
        path = self._base_config()
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["general", "dev", "ops"], {})
        self.assertEqual(added, ["general", "dev", "ops"])
        self.assertEqual(len(cfg.document["watchers"]), 1)
        self.assertEqual(cfg.document["watchers"][0]["rooms"], ["general", "dev", "ops"])

    def test_add_watcher_rooms_merges_into_an_existing_matching_entry(self):
        path = self._base_config("""\
              - connector: rc
                agent: default
                room: general
        """)
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["dev"], {})
        self.assertEqual(added, ["dev"])
        self.assertEqual(len(cfg.document["watchers"]), 1)
        self.assertEqual(cfg.document["watchers"][0]["rooms"], ["general", "dev"])

    def test_add_watcher_rooms_does_not_merge_when_shared_fields_differ(self):
        path = self._base_config("""\
              - connector: rc
                agent: default
                room: general
                online_notification: "hi"
        """)
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["dev"], {})
        self.assertEqual(added, ["dev"])
        self.assertEqual(len(cfg.document["watchers"]), 2)

    def test_add_watcher_rooms_does_not_merge_into_an_entry_with_its_own_name(self):
        """An entry with an explicit name:/session_id: can never legally
        become multi-room (gateway/config.py forbids it) — never a merge
        target, even if every other shared field matches."""
        path = self._base_config("""\
              - connector: rc
                agent: default
                room: general
                name: my-watcher
        """)
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["dev"], {})
        self.assertEqual(added, ["dev"])
        self.assertEqual(len(cfg.document["watchers"]), 2)

    def test_add_watcher_rooms_skips_a_room_already_in_the_merge_target(self):
        """User-requested: typing a room that's already present in the
        entry it would merge into is a silent no-op, not an error — the
        end state is identical either way."""
        path = self._base_config("""\
              - connector: rc
                agent: default
                rooms: [general, dev]
        """)
        cfg = EditableConfig.load(path)
        added = cfg.add_watcher_rooms("rc", "default", ["general", "ops"], {})
        self.assertEqual(added, ["ops"])  # 'general' silently skipped
        self.assertEqual(cfg.document["watchers"][0]["rooms"], ["general", "dev", "ops"])

    def test_add_watcher_rooms_deep_copies_shared_nested_values(self):
        """Regression: `shared`'s nested list/dict values (e.g.
        context_inject_files) must never alias into a newly-created entry —
        mutating one entry's nested value later must not silently affect
        another."""
        path = self._base_config()
        cfg = EditableConfig.load(path)
        shared = {"context_inject_files": ["a.md"]}
        cfg.add_watcher_rooms("rc", "default", ["general"], shared)
        cfg.document["watchers"][0]["context_inject_files"].append("b.md")
        self.assertEqual(shared["context_inject_files"], ["a.md"])  # untouched

    def test_find_mergeable_watcher_entry_ignores_connector_agent_defaults(self):
        """An entry relying on the implicit connector[0]/default_agent
        fallback (no explicit connector:/agent: of its own) is never a
        merge candidate — the fallback could shift later if connectors/
        agents are added or reordered."""
        path = self._base_config("""\
              - room: general
        """)
        cfg = EditableConfig.load(path)
        target = cfg.find_mergeable_watcher_entry("rc", "default", {})
        self.assertIsNone(target)

    def test_remove_watcher_room_normalizes_rooms_list_to_singular(self):
        path = self._base_config("""\
              - connector: rc
                agent: default
                rooms: [general, dev]
        """)
        cfg = EditableConfig.load(path)
        entry = cfg.document["watchers"][0]
        cfg.remove_watcher_room(entry, "dev")
        self.assertEqual(cfg.document["watchers"], [{"connector": "rc", "agent": "default", "room": "general"}])

    def test_remove_watcher_room_deletes_the_whole_entry_when_empty(self):
        path = self._base_config("""\
              - connector: rc
                agent: default
                room: general
        """)
        cfg = EditableConfig.load(path)
        entry = cfg.document["watchers"][0]
        cfg.remove_watcher_room(entry, "general")
        self.assertEqual(cfg.document["watchers"], [])

    def test_remove_watcher_room_is_a_no_op_for_an_unrelated_room(self):
        path = self._base_config("""\
              - connector: rc
                agent: default
                room: general
        """)
        cfg = EditableConfig.load(path)
        entry = cfg.document["watchers"][0]
        self.assertFalse(cfg.dirty)
        cfg.remove_watcher_room(entry, "does-not-exist")
        self.assertEqual(cfg.document["watchers"], [{"connector": "rc", "agent": "default", "room": "general"}])
        self.assertFalse(cfg.dirty)  # genuinely nothing changed — no spurious mark_dirty()

    def test_remove_watcher_room_is_a_no_op_for_a_room_not_in_the_group(self):
        """Same no-op guarantee as the singular-room case above, but for a
        multi-room `rooms:` list — a room that was never actually a member
        must not spuriously mark the config dirty or touch the list."""
        path = self._base_config("""\
              - connector: rc
                agent: default
                rooms: [general, dev]
        """)
        cfg = EditableConfig.load(path)
        entry = cfg.document["watchers"][0]
        cfg.remove_watcher_room(entry, "does-not-exist")
        self.assertEqual(entry["rooms"], ["general", "dev"])
        self.assertFalse(cfg.dirty)


class TestRoomlessEntriesAreNotMergeTargets(_EditableConfigTestBase):
    """A watcher entry with no room of its own must never absorb a new room.

    Such an entry is a live hazard rather than a curiosity.  It is invisible in
    the TUI — expanded_watchers() swallows the ValueError it raises, so it has no
    row and cannot be opened, edited or deleted — yet it is still in
    watchers_raw, and with none of the six shared keys _watcher_shared_fields()
    returns {}, which matched a fresh add's shared={}.

    Merging into it then reached disk, because save()'s gate blocks only errors
    a save *introduces* and merging a room into a roomless entry REMOVES its
    pre-existing error.  The result reads as a legal single-room entry, and every
    other room that should have had its own watcher is gone at the next start.
    """

    def _base_config(self, watchers_yaml: str = "") -> Path:
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
{watchers_yaml}
        """)

    ROOMLESS = """\
              - connector: rc
                agent: default
    """

    def test_a_roomless_entry_is_not_selected_as_a_merge_target(self):
        cfg = EditableConfig.load(self._base_config(self.ROOMLESS))

        self.assertIsNone(cfg.find_mergeable_watcher_entry("rc", "default", {}))

    def test_adding_a_room_creates_a_new_entry_instead_of_mutating_it(self):
        """The regression: the roomless entry must be left exactly as it was."""
        cfg = EditableConfig.load(self._base_config(self.ROOMLESS))

        added = cfg.add_watcher_rooms("rc", "default", ["general"], {})

        self.assertEqual(added, ["general"])
        watchers = cfg.document["watchers"]
        self.assertEqual(len(watchers), 2, "should have added a second entry")
        self.assertEqual(watchers[0], {"connector": "rc", "agent": "default"},
                         "the roomless entry was mutated")
        self.assertEqual(
            watchers[1], {"connector": "rc", "agent": "default", "room": "general"}
        )

    def test_an_entry_with_an_empty_rooms_list_is_also_skipped(self):
        """`rooms: []` fails the loader's non-empty check just as absence does."""
        cfg = EditableConfig.load(self._base_config("""\
              - connector: rc
                agent: default
                rooms: []
        """))

        self.assertIsNone(cfg.find_mergeable_watcher_entry("rc", "default", {}))

    def test_an_entry_with_an_empty_room_string_is_also_skipped(self):
        cfg = EditableConfig.load(self._base_config("""\
              - connector: rc
                agent: default
                room: ""
        """))

        self.assertIsNone(cfg.find_mergeable_watcher_entry("rc", "default", {}))

    def test_a_valid_entry_alongside_a_roomless_one_is_still_matched(self):
        """The guard must skip only the roomless entry, not disable merging."""
        cfg = EditableConfig.load(self._base_config("""\
              - connector: rc
                agent: default
              - connector: rc
                agent: default
                room: general
        """))

        target = cfg.find_mergeable_watcher_entry("rc", "default", {})

        self.assertIsNotNone(target)
        self.assertEqual(target["room"], "general")

    def test_merging_still_works_when_the_roomless_entry_comes_second(self):
        cfg = EditableConfig.load(self._base_config("""\
              - connector: rc
                agent: default
                room: general
              - connector: rc
                agent: default
        """))

        cfg.add_watcher_rooms("rc", "default", ["dev"], {})

        self.assertEqual(cfg.document["watchers"][0]["rooms"], ["general", "dev"])
        self.assertEqual(len(cfg.document["watchers"]), 2, "no third entry expected")


class TestWatcherSharedFields(unittest.TestCase):
    """_watcher_shared_fields() had no direct test, despite deciding merge
    eligibility — an entry whose result equals the caller's `shared` is adopted."""

    def test_an_entry_with_none_of_the_shared_keys_returns_empty(self):
        """This is what made a roomless entry match a fresh add's shared={}."""
        from gateway.configtool.model import _watcher_shared_fields

        self.assertEqual(_watcher_shared_fields({"connector": "rc", "agent": "d"}), {})

    def test_only_the_allowlisted_keys_are_returned(self):
        from gateway.configtool.model import _watcher_shared_fields

        got = _watcher_shared_fields({
            "connector": "rc", "agent": "d", "room": "general", "name": "x",
            "description": "desc", "inherits": "tpl",
            "context_inject_files": ["a.md"],
            "online_notification": "up", "offline_notification": "down",
            "history_handoff": {"enabled": True},
        })

        self.assertEqual(got, {
            "inherits": "tpl",
            "context_inject_files": ["a.md"],
            "online_notification": "up",
            "offline_notification": "down",
            "history_handoff": {"enabled": True},
            "description": "desc",
        })

    def test_absent_keys_are_omitted_rather_than_defaulted(self):
        from gateway.configtool.model import _watcher_shared_fields

        got = _watcher_shared_fields({"connector": "rc", "agent": "d", "description": "d"})

        self.assertEqual(got, {"description": "d"})



if __name__ == "__main__":
    unittest.main()
