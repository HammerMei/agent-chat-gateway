"""Hand the role-enforcement plugin to the ``opencode serve`` sidecar.

The plugin (``hooks/role-enforcement.ts``) is OpenCode's only view of AgentCoop's
role model: with it absent or stale, an owner session gets no approval prompts
for write tools — and OpenCode says nothing about it (a ``plugin`` entry pointing
at a missing file is listed in the merged config and then ignored silently).

The plugin reaches the sidecar as a ``file://`` entry in the ``plugin`` array of
``OPENCODE_CONFIG_CONTENT``, the environment variable the adapter already uses to
inject its bash permission defaults. Nothing is copied to disk: the sidecar loads
the copy shipped inside this package, so the plugin is the one this gateway's
code was written against, by construction, and the user's own ``opencode``
sessions are untouched. Verified against opencode 1.18.13 (#157):

- A ``file://`` entry supplied this way is merged *additively* with the user's
  own ``opencode.json`` ``plugin`` entries and with the ``plugins/`` directory
  scans (``$XDG_CONFIG_HOME/opencode/plugins/`` and ``~/.opencode/plugins/``).
- The module is evaluated and its default export invoked at ``opencode serve``
  bootstrap.
- ``GET /config`` on the running sidecar echoes the spec string back verbatim
  in ``plugin``. That is what the adapter checks after the health check: it
  proves opencode *accepted* the injected entry, not that the file loaded — a
  spec pointing at a missing file is listed just the same. The adapter checks
  :data:`PLUGIN_SOURCE` exists separately, before spawning.

The entry is injected unconditionally. What it enforces on 1.18.13 is the
guest allow-list (it throws for a ``COOP_ROLE=guest`` process — not the
sidecar, which is always owner). Its owner path sets ``output.status = "ask"``,
and opencode's ``tool.execute.before`` trigger discards the hook's output, so
that path gates nothing; the adapter's injected permission ruleset (``bash``,
``edit``, ``webfetch``, ``websearch`` → ``ask``) is what routes owner tools to
the gateway's broker, which always answers (ADR-0002, #165).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

PLUGIN_FILE_NAME = "role-enforcement.ts"

# The shipped copy. It lives inside the package, so it is present in any
# install that can import this module.
PLUGIN_SOURCE = Path(__file__).parent / "hooks" / PLUGIN_FILE_NAME

# The exact string handed to opencode and expected back from ``GET /config``.
# Resolved first so the URI has no symlink components (``/tmp`` vs
# ``/private/tmp`` on macOS) — opencode echoes what it was given, so the
# comparison is on this string, never on a reconstructed path.
PLUGIN_SPEC = PLUGIN_SOURCE.resolve().as_uri()


def inject_plugin_entry(config_content: str | None) -> str:
    """Return ``config_content`` (an ``OPENCODE_CONFIG_CONTENT`` JSON object, or
    empty) with :data:`PLUGIN_SPEC` present in its ``plugin`` array.

    Everything else in the object is preserved; the entry is appended once.

    Raises:
        ValueError: ``config_content`` is not a JSON object, or its ``plugin``
            key is not an array — appending to it would corrupt the user's
            config, so refuse rather than guess.
    """
    if config_content:
        try:
            config = json.loads(config_content)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"OPENCODE_CONFIG_CONTENT contains invalid JSON: {e}. "
                "Fix the env var or remove it."
            ) from e
        if not isinstance(config, dict):
            raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object.")
    else:
        config = {}

    plugins = config.setdefault("plugin", [])
    if not isinstance(plugins, list):
        raise ValueError(
            "OPENCODE_CONFIG_CONTENT's 'plugin' key must be an array; "
            f"found {type(plugins).__name__}."
        )
    if PLUGIN_SPEC not in plugins:
        plugins.append(PLUGIN_SPEC)
    return json.dumps(config)


def listed_plugin_copies(resolved_config: Mapping) -> list[str]:
    """Every entry in opencode's merged ``plugin`` array that names a
    ``role-enforcement.ts`` — ours and any other copy that will load too."""
    plugins = resolved_config.get("plugin")
    if not isinstance(plugins, list):
        return []
    return [p for p in plugins if isinstance(p, str) and p.endswith("/" + PLUGIN_FILE_NAME)]
