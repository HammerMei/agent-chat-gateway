"""Watcher runtime state: data model and persistence.

Moved from ``gateway.state`` into the core layer so that core modules
(``WatcherLifecycle``, ``InjectedContextBuilder``, ``StateStore``) can import it
without reaching up to the gateway application layer.

``gateway.state`` re-exports everything here for backward compatibility.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("agent-chat-gateway.state")

# Importing RUNTIME_DIR from the application layer would create a circular import
# (state.py is in core, runtime_lock.py is in the gateway package).
# We define it here directly — runtime_lock.py is the canonical definition;
# state.py keeps its own copy to avoid the cross-layer import.
RUNTIME_DIR = Path.home() / ".agent-chat-gateway"


def _state_file(connector_name: str) -> Path:
    """Return the state file path for the given connector name.

    Each connector gets its own namespaced file so multiple connectors
    can run side by side without clobbering each other's state.

    Example: connector_name="rc-home" → ~/.agent-chat-gateway/state.rc-home.json
    """
    return RUNTIME_DIR / f"state.{connector_name}.json"


@dataclass
class WatcherState:
    """Runtime state for a single watcher.  Persisted across gateway restarts."""

    watcher_name: str           # join key → WatcherConfig.name
    session_id: str             # auto-created session ID; "" when config owns the session ID
    room_id: str                # resolved room ID (cached)
    room_type: str = "channel"  # "channel", "group", or "dm"
    context_injected: bool = False  # True once all context files have been injected
    paused: bool = False            # True if paused via CLI
    last_processed_ts: str = ""     # ISO timestamp of last processed message
    # True for a watcher created by WatcherLifecycle.try_lazy_create()
    # (docs/design/on-the-fly-watchers.md) rather than a config.yaml entry.
    # sync_watchers() preserves entries flagged True across a restart even
    # though they're never in `_watcher_configs` at boot (PR #79 review) —
    # without this, a lazy watcher's session would be silently dropped by
    # the very next startup save, defeating the "resume a dormant session"
    # behavior after exactly one restart.
    dynamically_created: bool = False
    # Resolved agent name that provisioned `session_id` (PR #79 review).
    # Session IDs are backend-specific — if a wildcard rule's `agent:`
    # changes between restarts, a persisted session created under the OLD
    # agent must not be handed to the NEW one. "" (the default, and what
    # every state persisted before this field existed loads as) means
    # "unknown — assume compatible" so this doesn't force-reset every
    # already-running watcher's session the first restart after this ships.
    agent: str = ""


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def load_state(connector_name: str) -> list[WatcherState]:
    """Load watcher runtime state for the given connector from disk.

    Supports two formats:
      - New format: records have a 'watcher_name' key.
      - Legacy format: records have a 'watcher_id' key (old WatcherInfo schema).
        Legacy records are migrated: watcher_name is set to room_name (best-effort).
    """
    ensure_runtime_dir()
    state_file = _state_file(connector_name)
    if not state_file.exists():
        return []
    try:
        data = json.loads(state_file.read_text())
        watchers = []
        for w in data.get("watchers", []):
            if "watcher_name" in w:
                # New format
                watchers.append(WatcherState(
                    watcher_name=w["watcher_name"],
                    session_id=w.get("session_id", ""),
                    room_id=w.get("room_id", ""),
                    room_type=w.get("room_type", "channel"),
                    context_injected=w.get("context_injected", False),
                    paused=w.get("paused", False),
                    last_processed_ts=w.get("last_processed_ts", ""),
                    dynamically_created=w.get("dynamically_created", False),
                    agent=w.get("agent", ""),
                ))
            elif "watcher_id" in w:
                # Legacy format — migrate best-effort using room_name as watcher_name
                watcher_name = w.get("room_name", w["watcher_id"])
                logger.info(
                    "Migrating legacy state entry watcher_id=%s → watcher_name=%s",
                    w["watcher_id"][:8], watcher_name,
                )
                watchers.append(WatcherState(
                    watcher_name=watcher_name,
                    session_id=w.get("session_id", ""),
                    room_id=w.get("room_id", ""),
                    room_type=w.get("room_type", "channel"),
                    context_injected=w.get("context_injected", False),
                    paused=False,
                    last_processed_ts=w.get("last_processed_ts", ""),
                ))
        logger.info(
            "[%s] Loaded %d watcher states from disk", connector_name, len(watchers)
        )
        return watchers
    except Exception as e:
        logger.warning(
            "[%s] Failed to load state file, starting fresh: %s", connector_name, e
        )
        return []


def save_state(connector_name: str, watchers: list[WatcherState]) -> None:
    """Save watcher runtime state for the given connector to disk.

    Uses an atomic write pattern (write to .tmp then rename) so a crash or
    interruption during the write can never leave a partially-written JSON file.
    The rename(2) syscall is atomic on POSIX when src and dst are on the same
    filesystem, which is guaranteed here because both paths are under RUNTIME_DIR.
    """
    ensure_runtime_dir()
    state_file = _state_file(connector_name)
    # Use a PID-unique temp name to avoid two concurrent writers clobbering
    # each other's tmp file.
    tmp_file = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")
    data = {"watchers": [asdict(w) for w in watchers]}
    try:
        tmp_file.write_text(json.dumps(data, indent=2))
        tmp_file.replace(state_file)
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise
    logger.debug("[%s] Saved %d watcher states to disk", connector_name, len(watchers))
