"""Sync tests for gateway/schema/config.schema.json.

The hand-written JSON Schema is not generated from the dataclasses/parser in
gateway/config.py, so it can silently drift from the format the parser
actually accepts. These tests are the drift tripwire: they validate the
canonical example config and the e2e fixture (both exercised elsewhere by
GatewayConfig.from_file-style tests) against the schema, and spot-check a
handful of known-invalid documents to confirm the schema is not accidentally
too permissive to catch anything at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = REPO_ROOT / "gateway" / "schema" / "config.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator(schema: dict) -> jsonschema.Draft202012Validator:
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class TestSchemaIsValid:
    def test_schema_is_valid_draft_2020_12(self, schema):
        # Raises if the schema document itself is malformed.
        jsonschema.Draft202012Validator.check_schema(schema)


class TestExampleAndFixtureConfigsMatchSchema:
    """The two configs the gateway actually loads elsewhere in the test suite
    must validate cleanly — if this fails, either the schema drifted from a
    parser change, or the example/fixture drifted from the documented format."""

    def test_config_example_yaml_is_schema_valid(self, validator):
        doc = _load_yaml(REPO_ROOT / "config.example.yaml")
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    def test_e2e_fixture_config_is_schema_valid(self, validator):
        doc = _load_yaml(REPO_ROOT / "tests" / "e2e" / "acg-config" / "config.yaml")
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    def test_description_field_is_schema_valid_everywhere(self, validator):
        """'description:' is accepted on connectors, agents, watchers, and all
        three *_templates blocks — additionalProperties: false on agent/watcher
        means this must be explicit in the schema, not just implicitly allowed."""
        doc = _load_yaml(REPO_ROOT / "config.example.yaml")
        doc = copy.deepcopy(doc)
        doc["connectors"][0]["description"] = "Primary bot"
        doc["agents"]["my-agent"]["description"] = "The main agent"
        doc["watchers"][0]["description"] = "General channel"
        doc["connector_templates"] = {"x": {"description": "Shared connector settings"}}
        doc["agent_templates"] = {"x": {"description": "Shared agent settings"}}
        doc["watcher_templates"] = {"x": {"description": "Shared watcher settings"}}
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)


class TestSchemaCatchesKnownMistakes:
    """Negative controls — if these stop failing, the schema became too
    permissive (e.g. a stray additionalProperties: true) to catch anything."""

    @pytest.fixture
    def base_doc(self) -> dict:
        return _load_yaml(REPO_ROOT / "config.example.yaml")

    def test_typo_top_level_key_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["watchres"] = bad.pop("watchers")
        assert list(validator.iter_errors(bad))

    def test_room_and_rooms_both_set_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["watchers"][0]["room"] = "oops"
        assert list(validator.iter_errors(bad))

    def test_typo_in_tool_rule_key_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["agents"]["my-agent"]["owner_allowed_tools"].append({"toool": "Read"})
        assert list(validator.iter_errors(bad))

    def test_unknown_connector_type_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["connectors"][0]["type"] = "rocketchatt"
        assert list(validator.iter_errors(bad))

    def test_connector_template_setting_name_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["connector_templates"] = {"x": {"name": "not-allowed"}}
        assert list(validator.iter_errors(bad))

    def test_watcher_template_setting_session_id_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["watcher_templates"] = {"x": {"session_id": "not-allowed"}}
        assert list(validator.iter_errors(bad))

    def test_template_setting_inherits_is_rejected(self, validator, base_doc):
        """No nested templates — a template cannot itself set 'inherits'."""
        bad = copy.deepcopy(base_doc)
        bad["agent_templates"] = {"x": {"inherits": "y"}}
        assert list(validator.iter_errors(bad))
