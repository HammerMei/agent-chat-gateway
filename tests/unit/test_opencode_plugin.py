"""Tests for gateway.agents.opencode.plugin — handing the role-enforcement plugin
to the sidecar through ``OPENCODE_CONFIG_CONTENT`` (#157).

Verified against opencode 1.18.13: a ``file://`` entry in the ``plugin`` array
of ``OPENCODE_CONFIG_CONTENT`` is merged *additively* with the user's own
``opencode.json`` plugin entries and with the ``plugins/`` directory scans, the
module is evaluated and its default export invoked at ``opencode serve``
bootstrap, and ``GET /config`` echoes the exact spec string back in ``plugin``.
"""

from __future__ import annotations

import json

import pytest

from gateway.agents.opencode.plugin import (
    PLUGIN_SOURCE,
    PLUGIN_SPEC,
    inject_plugin_entry,
    listed_plugin_copies,
)


class TestPluginSpec:
    def test_spec_is_a_file_uri_to_the_shipped_plugin(self):
        assert PLUGIN_SPEC.startswith("file:///")
        assert PLUGIN_SPEC.endswith("/role-enforcement.ts")
        assert PLUGIN_SOURCE.is_file()


class TestInjectPluginEntry:
    def test_none_config_becomes_config_with_only_the_plugin(self):
        assert json.loads(inject_plugin_entry(None)) == {"plugin": [PLUGIN_SPEC]}

    def test_empty_string_is_treated_as_no_config(self):
        assert json.loads(inject_plugin_entry("")) == {"plugin": [PLUGIN_SPEC]}

    def test_appends_to_existing_plugins_and_preserves_other_keys(self):
        existing = json.dumps({
            "plugin": ["some-npm-plugin"],
            "permission": {"bash": {"*": "allow"}},
        })

        result = json.loads(inject_plugin_entry(existing))

        assert result["plugin"] == ["some-npm-plugin", PLUGIN_SPEC]
        assert result["permission"] == {"bash": {"*": "allow"}}

    def test_idempotent(self):
        once = inject_plugin_entry(None)
        twice = inject_plugin_entry(once)
        assert json.loads(twice)["plugin"].count(PLUGIN_SPEC) == 1

    def test_malformed_json_raises_value_error_naming_the_variable(self):
        with pytest.raises(ValueError, match="OPENCODE_CONFIG_CONTENT"):
            inject_plugin_entry("{not json")

    def test_plugin_key_that_is_not_a_list_raises(self):
        """A string where opencode expects an array is the user's mistake, and
        appending to it would corrupt their config — refuse rather than guess."""
        with pytest.raises(ValueError, match="plugin"):
            inject_plugin_entry(json.dumps({"plugin": "not-a-list"}))


class TestListedPluginCopies:
    def test_returns_every_role_enforcement_entry(self):
        resolved = {"plugin": [
            "file:///Users/u/.opencode/plugins/role-enforcement.ts",
            "file:///Users/u/.opencode/plugins/memory-bootstrap.ts",
            PLUGIN_SPEC,
            "some-npm-plugin",
        ]}
        assert listed_plugin_copies(resolved) == [
            "file:///Users/u/.opencode/plugins/role-enforcement.ts",
            PLUGIN_SPEC,
        ]

    def test_missing_or_non_list_plugin_key_is_empty(self):
        assert listed_plugin_copies({}) == []
        assert listed_plugin_copies({"plugin": None}) == []
        assert listed_plugin_copies({"plugin": "oops"}) == []
