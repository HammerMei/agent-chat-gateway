"""upsert_env_vars() — merge-by-key `.env` writer for the config TUI.

`gateway/config.py`'s loader calls `load_dotenv(path.parent / ".env")` —
the `.env` file lives beside `config.yaml`, not at some fixed install
location, so a `.env` write triggered by the TUI must resolve the same way
(`EditableConfig.path.parent / ".env"`), matching `working_directory`'s own
"relative to config_dir" convention.

`gateway/onboard.py`'s existing `_write_env()` unconditionally overwrites
the whole file with 3 hardcoded keys (RC_URL/RC_USERNAME/RC_PASSWORD) — fine
for the onboarding wizard's one-shot Rocket.Chat setup, but wrong here: the
config TUI's per-field "store in .env" toggle (docs/design/config-tool.md
decision 6) needs to upsert ONE secret at a time without clobbering every
other connector's already-written `.env` entries. `upsert_env_vars()` merges
by key instead — every other line (comments, blank lines, unrelated keys) is
preserved exactly as-is, in its original position.
"""

from __future__ import annotations

from pathlib import Path


def _quote(value: str) -> str:
    # Matches onboard.py's own _write_env() convention exactly, so a value
    # written by either path looks the same on disk.
    return f'"{value}"' if " " in value else value


def upsert_env_vars(env_path: Path, updates: dict[str, str]) -> None:
    """Merge `updates` (key -> plain, unquoted value) into the `.env` file
    at `env_path`, creating it (and its parent directory) if it doesn't
    exist yet. A key that already has a `KEY=...` line gets that line
    REPLACED in place (preserving its position); a key not already present
    is appended at the end. Every other line is untouched. Restricts the
    file to 0600 after writing (matching onboard.py's convention — this
    file holds secrets).
    """
    remaining = dict(updates)
    lines: list[str] = []
    if env_path.exists():
        for raw_line in env_path.read_text().splitlines():
            stripped = raw_line.strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key and not stripped.startswith("#") and key in remaining:
                lines.append(f"{key}={_quote(remaining.pop(key))}")
            else:
                lines.append(raw_line)
    for key, value in remaining.items():
        lines.append(f"{key}={_quote(value)}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(("\n".join(lines) + "\n") if lines else "")
    env_path.chmod(0o600)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def read_env_vars(env_path: Path) -> dict[str, str]:
    """Read the current `KEY=value` pairs from `.env` at `env_path` (empty
    dict if it doesn't exist). Used to check whether a key already exists —
    and with what value — before `upsert_env_vars()` would silently
    overwrite it (see `ConnectorDetailScreen.action_save()`'s pre-write
    collision check)."""
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


def remove_env_vars(env_path: Path, keys: set[str]) -> None:
    """Remove any `KEY=...` line whose key is in `keys` from `.env` at
    `env_path`. A no-op if the file doesn't exist or none of `keys` are
    present. Every other line — comments, blank lines, unrelated keys — is
    preserved exactly as-is, in its original position. Restricts the file
    to 0600 after writing, same as `upsert_env_vars()`."""
    if not env_path.exists():
        return
    lines: list[str] = []
    changed = False
    for raw_line in env_path.read_text().splitlines():
        stripped = raw_line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key and not stripped.startswith("#") and key in keys:
            changed = True
            continue
        lines.append(raw_line)
    if not changed:
        return
    env_path.write_text(("\n".join(lines) + "\n") if lines else "")
    env_path.chmod(0o600)
