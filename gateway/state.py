"""Re-exports from gateway.core.state for backward compatibility.

The canonical definitions now live in ``gateway.core.state`` so that core
modules can import them without reaching up to the gateway application layer.
External code that imports from ``gateway.state`` continues to work via
these re-exports.
"""

from .core.state import (  # noqa: F401 — re-exports
    RUNTIME_DIR,
    WatcherState,
    ensure_runtime_dir,
    load_state,
    save_state,
)
