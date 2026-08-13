"""Watcher runtime state: data model and persistence.

Moved from ``gateway.state`` into the core layer so that core modules
(``WatcherLifecycle``, ``InjectedContextBuilder``, ``StateStore``) can import it
without reaching up to the gateway application layer.

``gateway.state`` re-exports everything here for backward compatibility.

**On-disk format is versioned, and an unversioned file is refused rather than
converted** — see ``load_state`` and ``LegacyStateError``.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("agent-chat-gateway.state")

# Importing RUNTIME_DIR from the application layer would create a circular import
# (state.py is in core, runtime_lock.py is in the gateway package).
# We define it here directly — runtime_lock.py is the canonical definition;
# state.py keeps its own copy to avoid the cross-layer import.
RUNTIME_DIR = Path.home() / ".agent-chat-gateway"

# Current on-disk format. Bumped when a record gains fields that cannot be
# defaulted from an older file — which is why this exists at all: the fields added
# for on-the-fly watchers (the materialized config, the originating rule, the
# backend identity) have no honest default, so a file without them cannot be read
# as if it had them.
STATE_FORMAT_VERSION = 2


class StateFormatError(Exception):
    """A state file this build cannot read.

    Deliberately not a subclass of anything ``load_state``'s own ``except`` clause
    catches: a refusal that gets swallowed and turned into "starting fresh" is the
    precise failure this exists to prevent (design §5.3). Every caller of
    ``load_state`` has to decide about it explicitly.

    Split into two subclasses because the two directions need *opposite* advice, and
    a message that gives the wrong one is worse than no message: an older file should
    be deleted, and a newer file must not be.
    """

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(message)


class LegacyStateError(StateFormatError):
    """The file predates ``STATE_FORMAT_VERSION`` (or has no version marker)."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(
            path,
            f"State file '{path}' is in an older format this version cannot read "
            f"({detail}). There is no automatic conversion: an old record carries no "
            "agent, no materialized config and no originating rule, so converting it "
            "would have to guess which rule now owns the room — the silent re-binding "
            "the design exists to prevent (docs/design/dynamic-watcher-design.md "
            "§5.3).\n"
            "To proceed: stop the gateway, run 'agent-chat-gateway list' to record "
            "what exists and what is paused, move the state file(s) aside or delete "
            f"them ('{path}'), then start again. Your config.yaml does NOT need "
            "rewriting for this: rule-shaped watchers are not active yet, so the "
            "§5.3 procedure's config rewrite belongs to the later cutover, not to "
            "this step.\n"
            "Accepted loss: each room starts a fresh agent session (history handoff "
            "refetches recent room messages, so there is partial continuity), the "
            "message watermark resets once per room, and any paused watcher comes "
            "back active — re-pause it after start."
        )


class FutureStateError(StateFormatError):
    """The file was written by a newer build than this one.

    Kept separate from the legacy case on purpose. The legacy message says to delete
    the file; following that here — during a rollback, say — would destroy perfectly
    valid state *and* still leave this build unable to read it.
    """

    def __init__(self, path: Path, version: object) -> None:
        super().__init__(
            path,
            f"State file '{path}' was written by a newer version of the gateway "
            f"(format {version!r}; this build reads {STATE_FORMAT_VERSION}). "
            "**Do not delete it** — it holds valid sessions, and deleting it would "
            "not make this build able to read them. Either run the newer version "
            "again, or, if this is an intentional rollback and losing those sessions "
            f"is acceptable, move '{path}' aside first so it can be restored."
        )


def _state_file(connector_name: str) -> Path:
    """Return the state file path for the given connector name.

    Each connector gets its own namespaced file so multiple connectors
    can run side by side without clobbering each other's state.

    Example: connector_name="rc-home" → ~/.agent-chat-gateway/state.rc-home.json
    """
    return RUNTIME_DIR / f"state.{connector_name}.json"


@dataclass
class WatcherState:
    """Runtime state for a single watcher.  Persisted across gateway restarts.

    Every field here has to be written in two places — this dataclass and
    ``load_state``'s reader — and each addition ships with a round-trip test, since
    this on-disk surface had no serialization test at all before (design §5.3).
    """

    watcher_name: str           # join key → WatcherConfig.name
    session_id: str             # session id assigned by the agent backend; "" = none yet
    room_id: str                # resolved room ID (cached)
    room_type: str = "channel"  # "channel", "group", or "dm"
    context_injected: bool = False  # True once all context files have been injected
    paused: bool = False            # True if paused via CLI
    last_processed_ts: str = ""      # ISO timestamp of last processed message

    # ── On-the-fly watcher fields (design §5.3) ──────────────────────────────
    # Written by the watcher manager; empty on records the static path creates,
    # which is why every one of them defaults rather than being required.

    # The platform's own name for the room, refreshed from inbound messages.
    # Empty for DMs, which have no name to carry.
    room_name: str = ""
    # channel / group / dm / group_dm — decides the label form and whether
    # require_mention applies (§2.7). Distinct from `room_type` above, which is the
    # connector's own three-way type and predates the group-DM distinction.
    room_kind: str = ""
    # DM counterparts, for the `list` column. Refreshed, and never part of a key:
    # a member set is not an identity (§6.4).
    participants: list[str] = field(default_factory=list)
    # So a rule edit cannot silently re-point a dormant session at another
    # connector or agent.
    connector: str = ""
    agent: str = ""
    # The resolved backend type + working directory this session was created
    # against, compared before the stored session_id is reused. A mismatch means
    # the id would be replayed into a different session store, so it forces a
    # fresh session instead (§2.4).
    backend_identity: str = ""
    created_at: str = ""          # audit
    last_activity_at: str = ""    # the idle clock (§2.5)
    # Distinguishes was-active from was-idle at boot. Empty = was active.
    dropped_at: str = ""
    # The materialized watcher config used to recreate this watcher, and the rule
    # it came from. Nested structures, not scalars — which is what the round-trip
    # test has to cover for nesting and for the empty case.
    config: dict = field(default_factory=dict)
    rule_name: str = ""
    # The originating rule as resolved at creation: the drift baseline (§2.4).
    rule: dict = field(default_factory=dict)


# Every field the reader below restores, so a field added to the dataclass without
# a reader entry fails a test rather than silently loading as its default forever.
# Scalars only; the two nested fields and the list are handled beside it.
_SCALAR_FIELDS: tuple[tuple[str, object], ...] = (
    ("session_id", ""),
    ("room_id", ""),
    ("room_type", "channel"),
    ("context_injected", False),
    ("paused", False),
    ("last_processed_ts", ""),
    ("room_name", ""),
    ("room_kind", ""),
    ("connector", ""),
    ("agent", ""),
    ("backend_identity", ""),
    ("created_at", ""),
    ("last_activity_at", ""),
    ("dropped_at", ""),
    ("rule_name", ""),
)


def state_files() -> list[Path]:
    """Every persisted state file on disk, whichever connectors currently exist.

    Enumerating the *files* rather than the configured connectors is the point. A
    connector renamed or removed in config.yaml leaves `state.<old-name>.json` behind,
    and a caller that iterates `config.connectors` never opens it — so a legacy file
    belonging to a since-renamed connector would sail past the refusal and be
    abandoned silently, which is the exact failure the refusal exists to prevent.
    """
    ensure_runtime_dir()
    return sorted(RUNTIME_DIR.glob("state.*.json"))


def check_state_formats() -> None:
    """Raise on the first state file this build cannot read.

    A preflight for callers that must not proceed past an unreadable file — the daemon
    on startup, and `config validate`. Reading each file is cheap next to the cost of
    the alternative: booting with an empty registry and abandoning every session while
    looking successful.
    """
    for path in state_files():
        # state.<name>.json → <name>. rsplit, because a connector name may contain
        # dots; only the leading "state." and trailing ".json" are ours.
        connector_name = path.name[len("state."):-len(".json")]
        load_state(connector_name)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _record_from_dict(w: dict) -> WatcherState:
    """Build a WatcherState from one persisted record."""
    return WatcherState(
        watcher_name=w["watcher_name"],
        participants=list(w.get("participants") or []),
        config=dict(w.get("config") or {}),
        rule=dict(w.get("rule") or {}),
        **{name: w.get(name, default) for name, default in _SCALAR_FIELDS},
    )


def load_state(connector_name: str) -> list[WatcherState]:
    """Load watcher runtime state for the given connector from disk.

    Raises:
        LegacyStateError: If the file predates ``STATE_FORMAT_VERSION``.
        FutureStateError: If it was written by a newer build. This is a
            version check, not a converter — the legacy reader was deleted rather
            than extended, because the fields added for on-the-fly watchers cannot
            be reconstructed from an old record (design §5.3). Refusing is the point:
            the alternative is booting with an empty registry, which abandons every
            session and looks like a successful start.

    A missing file is not an error (first run). A corrupted or unreadable one is
    still handled by starting fresh, unchanged — it carries no recoverable state
    either way, so refusing to boot over it would trade a graceful degradation for
    an outage.
    """
    ensure_runtime_dir()
    state_file = _state_file(connector_name)
    if not state_file.exists():
        return []
    try:
        data = json.loads(state_file.read_text())
    except (OSError, ValueError, RecursionError) as e:
        # RecursionError, not just ValueError: json.loads raises it on deeply nested
        # input (~100k levels), which is corruption by any reasonable reading. Omitting
        # it would let a corrupt file abort startup — regressing the "corrupted files
        # start fresh" contract this function documents two paragraphs up, which is
        # precisely the kind of contradiction between a docstring and its code that a
        # narrowed `except` invites.
        logger.warning(
            "[%s] Failed to read state file, starting fresh: %s", connector_name, e
        )
        return []

    # The version check runs on parsed content and outside the try above, because a
    # legacy file is perfectly valid JSON: catching it here would convert the
    # refusal back into the silent "starting fresh" it exists to replace.
    if not isinstance(data, dict):
        logger.warning(
            "[%s] State file is not a JSON object, starting fresh", connector_name
        )
        return []
    version = data.get("version")
    if version != STATE_FORMAT_VERSION:
        if isinstance(version, int) and version > STATE_FORMAT_VERSION:
            raise FutureStateError(state_file, version)
        raise LegacyStateError(
            state_file,
            f"format {version!r}" if version is not None else "no version marker",
        )

    try:
        watchers = [
            _record_from_dict(w)
            for w in data.get("watchers", [])
            if isinstance(w, dict) and "watcher_name" in w
        ]
    except (TypeError, ValueError) as e:
        logger.warning(
            "[%s] Malformed record in state file, starting fresh: %s",
            connector_name, e,
        )
        return []
    logger.info(
        "[%s] Loaded %d watcher states from disk", connector_name, len(watchers)
    )
    return watchers


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
    data = {
        "version": STATE_FORMAT_VERSION,
        "watchers": [asdict(w) for w in watchers],
    }
    try:
        tmp_file.write_text(json.dumps(data, indent=2))
        tmp_file.replace(state_file)
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise
    logger.debug("[%s] Saved %d watcher states to disk", connector_name, len(watchers))
