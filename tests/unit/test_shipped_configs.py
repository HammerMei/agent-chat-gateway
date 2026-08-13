"""Every config the project ships must still load.

This exists because of a specific miss. Removing `watchers[].session_id` was swept
through `gateway/`, `tests/` and `docs/` — and not through `docker/`, where two
shipped inputs still set it:

* `docker/docker-compose.example/config/config.yaml` (`session_id: ~`), the
  ready-to-copy config for the documented compose path
* `docker/entrypoint.acg.sh`'s env-var quick-start generator (`"session_id": None`)

Both would have failed at startup, so **every new Docker install would have broken**,
which is worse than any of the documentation problems found alongside it. A review
caught it; nothing in the repository would have.

`config.example.yaml` was already schema-validated by `test_config_schema.py`. The
gap was that no test knew the Docker inputs existed. This closes it by enumerating
the shipped inputs rather than by remembering to check them: a new bootstrap config
added without a line here fails the completeness test below.

Run with:
    uv run python -m pytest tests/unit/test_shipped_configs.py -v
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import unittest
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "gateway" / "schema" / "config.schema.json"

# Every config.yaml the project hands a user or generates for them.
SHIPPED_CONFIGS = (
    REPO / "config.example.yaml",
    REPO / "docker" / "docker-compose.example" / "config" / "config.yaml",
    REPO / "tests" / "e2e" / "acg-config" / "config.yaml",
)

# Scripts that generate a config.yaml at runtime, and cannot be schema-validated
# without executing them.
GENERATOR_SCRIPTS = (REPO / "docker" / "entrypoint.acg.sh",)


def _validator() -> jsonschema.Draft202012Validator:
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


class TestShippedConfigsValidate(unittest.TestCase):
    """The schema is the cheapest gate that catches a removed key in shipped input.

    It is not enforced at load — only tests run it — which is exactly why the shipped
    inputs need a test that does. `$defs/staticWatcher` sets
    `additionalProperties: false`, so a field removed from the schema makes any config
    still carrying it fail here.
    """

    def test_every_shipped_config_matches_the_schema(self):
        validator = _validator()
        for path in SHIPPED_CONFIGS:
            with self.subTest(config=str(path.relative_to(REPO))):
                self.assertTrue(path.exists(), f"{path} is listed but missing")
                with open(path) as f:
                    doc = yaml.safe_load(f)
                errors = list(validator.iter_errors(doc))
                self.assertFalse(
                    errors,
                    f"{path.relative_to(REPO)}:\n"
                    + "\n".join(str(e) for e in errors),
                )

    def test_the_shipped_config_list_is_complete(self):
        """A bootstrap config added elsewhere must be added here too, or this sweep
        quietly stops covering it — the failure mode that let the Docker configs drift.

        "Shipped" means git-tracked. A directory blocklist was the first attempt and
        was wrong in both directions: it swept a developer's own gitignored
        `config.yaml` and stale `.claude/worktrees/` copies, while still relying on
        someone predicting every directory worth excluding. What git tracks is the
        actual definition of what users receive.
        """
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "*config.yaml"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
        found = {
            (REPO / name).resolve()
            for name in tracked.split("\0")
            if name.endswith("config.yaml")
        }
        self.assertTrue(found, "git ls-files matched nothing — wrong cwd?")
        listed = {p.resolve() for p in SHIPPED_CONFIGS}
        self.assertEqual(
            found - listed,
            set(),
            "a git-tracked config.yaml is not covered by SHIPPED_CONFIGS",
        )


class TestGeneratedConfigsCarryNoRemovedKeys(unittest.TestCase):
    """A generator script builds its config as a literal, so it can be read statically.

    Executing `entrypoint.acg.sh` is not an option here (it needs a container), but the
    embedded Python builds one dict literal, so the watcher keys it emits can be
    extracted and checked against the schema's own field list — no hand-maintained copy
    of "which keys are gone".
    """

    def _embedded_python(self, script: Path) -> list[str]:
        return re.findall(r"<< 'PYEOF'\n(.*?)\nPYEOF", script.read_text(), re.S)

    def test_the_generator_emits_only_declared_watcher_keys(self):
        with open(SCHEMA_PATH) as f:
            schema = json.load(f)
        allowed = set(schema["$defs"]["staticWatcher"]["properties"])

        for script in GENERATOR_SCRIPTS:
            blocks = self._embedded_python(script)
            self.assertTrue(blocks, f"no embedded python found in {script.name}")
            for block in blocks:
                # Compiling first turns an edit that breaks the script into a failure
                # here rather than at container start.
                compile(block, str(script), "exec")
                tree = ast.parse(block)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Dict):
                        continue
                    keys = {
                        k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    # Identify a watcher dict by a key only watchers have.
                    if "room" not in keys and "rooms" not in keys:
                        continue
                    unknown = keys - allowed
                    self.assertEqual(
                        unknown,
                        set(),
                        f"{script.name} emits watcher key(s) the schema no longer "
                        f"declares: {sorted(unknown)}",
                    )


if __name__ == "__main__":
    unittest.main()
