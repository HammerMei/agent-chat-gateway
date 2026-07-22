"""read_env_vars() — read a `.env` file's `KEY=value` pairs.

`gateway/config.py`'s loader calls `load_dotenv(path.parent / ".env")` — the
`.env` file lives beside `config.yaml`, not at some fixed install location,
so a read triggered by the config tool must resolve the same way
(`EditableConfig.path.parent / ".env"`), matching `working_directory`'s own
"relative to config_dir" convention.

Used by `gateway/configtool/screens/form_common.py`'s `_resolve_secret_
display()` to show the real secret behind an existing `$VAR`/`${VAR}`
reference for display/editing, and by `gateway/config_migrate.py`'s
one-time migration (docs/design/config-tool.md decision 6 revisited: the
config tool no longer WRITES `.env` — this module used to also hold
`upsert_env_vars()`/`remove_env_vars()` for that, removed once nothing
called them anymore).
"""

from __future__ import annotations

from pathlib import Path


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def read_env_vars(env_path: Path) -> dict[str, str]:
    """Read the current `KEY=value` pairs from `.env` at `env_path` (empty
    dict if it doesn't exist)."""
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = _unquote(value.strip())
    return result
