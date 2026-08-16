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
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import Flag, auto
from pathlib import Path
from typing import get_origin

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

# The config schema a frozen `rule`/`config` snapshot was taken under (§2.4). A *second*
# number, and deliberately not the one above: that one answers "can this build read these
# records at all" and is enforced by refusing to start, while this one answers "what did
# this snapshot's fields mean when it was written". They change for different reasons, and
# the whole point of the second is that a config-schema change must not refuse a file whose
# records are perfectly readable.
#
# Owned by code and stamped into each record when the snapshot is frozen — never written by
# an operator, so it cannot lie. Per-record rather than per-file because one state file
# legitimately holds watchers frozen either side of a schema change; a file-level stamp
# would be rewritten on every save and would silently relabel old snapshots as current.
CONFIG_SCHEMA_VERSION = 1


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
            f"To proceed: move '{path}' aside — keep the copy, because the file IS "
            "the inventory: it lists each watcher's name, session id, paused flag and "
            "message watermark in plain JSON. Then start again. Do not reach for "
            "'agent-chat-gateway list' at this point: it queries the running daemon, "
            "and the daemon is what just refused to start. Your config.yaml does NOT need "
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
    # Which config schema the two snapshots above were written under. 0 means "no snapshot"
    # — a record that predates rule-derived creation, or one written by the static path.
    config_schema_version: int = 0


class StateFilter(Flag):
    """Which lifecycle states a ``list`` should return (design §2.8).

    Composable, and the default is deliberately not ``ALL``: ``OPERABLE`` is the
    set an operator is realistically about to act on.  A paused watcher belongs
    in the default view precisely because it is waiting on a human decision;
    idle is informational — the bot knows about the room, but nothing is running
    and nothing is being withheld.  Once membership events register joined rooms
    as idle, a bot in two hundred channels would otherwise have a ``list``
    dominated by rooms nobody has ever spoken in.
    """

    ACTIVE = auto()
    IDLE = auto()
    PAUSED = auto()
    OPERABLE = ACTIVE | PAUSED
    ALL = ACTIVE | IDLE | PAUSED


# The wire spelling of each individual state, and the only place the two
# vocabularies meet.  Deriving the reverse map rather than writing it out keeps a
# new state from being addable to one direction alone.
STATE_FILTER_NAMES: dict[StateFilter, str] = {
    StateFilter.ACTIVE: "active",
    StateFilter.IDLE: "idle",
    StateFilter.PAUSED: "paused",
}
_NAMES_TO_STATE_FILTER = {name: flag for flag, name in STATE_FILTER_NAMES.items()}


def lifecycle_state(record: "WatcherState") -> StateFilter:
    """Return the one lifecycle state ``record`` is in.

    Three answers, one per record, and the precedence is the point:

    * **paused wins over everything.**  It is an operator's explicit decision,
      and §4.4 has even ``get`` refuse to override it.  A record that is both
      paused and dropped is still awaiting a human, so reporting it as idle
      would hide the only one of the two that someone has to act on.
    * **idle** is a record the manager dropped — ``dropped_at`` is written when
      the watcher is released and cleared when it is recreated (§2.5).
    * **active** otherwise.

    Written as a named function with one answer per case rather than as a
    condition at each call site: a rule with three answers spelled as a boolean
    expression is invisible in a diff when the clause that fires is not the
    first one.
    """
    if record.paused:
        return StateFilter.PAUSED
    if record.dropped_at:
        return StateFilter.IDLE
    return StateFilter.ACTIVE


def state_filter_name(state: StateFilter) -> str:
    """Return the wire/display spelling of a single lifecycle state."""
    return STATE_FILTER_NAMES[state]


def parse_state_filter(names: list[str] | None) -> StateFilter:
    """Build a ``StateFilter`` from wire state names; ``None`` means the default.

    An unrecognised name raises rather than being dropped: a filter that
    silently ignores what it was asked for answers a different question than
    the one asked, and the caller cannot tell from the result.
    """
    if names is None:
        return StateFilter.OPERABLE
    result = StateFilter(0)
    for name in names:
        try:
            result |= _NAMES_TO_STATE_FILTER[name]
        except (KeyError, TypeError):
            known = ", ".join(sorted(_NAMES_TO_STATE_FILTER))
            raise ValueError(
                f"unknown watcher state {name!r} — expected one of: {known}"
            ) from None
    if not result:
        raise ValueError("state filter is empty — name at least one state")
    return result


# The type each persisted field must have, derived from WatcherState's own annotations
# so there is no second hand-maintained table to drift out of step. `watcher_name` is
# excluded: it is required rather than defaulted, and checked separately.
_FIELD_TYPES: dict[str, type] = {
    # get_origin() unwraps a parameterised annotation (list[str] -> list) and returns
    # None for a plain one, so this needs no name-to-type table of its own — the first
    # attempt used one, and it raised KeyError on `list[str]` because its `__name__`
    # is "list", not "list[str]".
    f.name: get_origin(f.type) or f.type
    for f in fields(WatcherState)
    if f.name != "watcher_name"
}

# Fields with neither a default nor a default_factory: the dataclass requires them, so
# the reader has to supply something when the payload omits them.
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    f.name
    for f in fields(WatcherState)
    if f.name != "watcher_name"
    and f.default is MISSING
    and f.default_factory is MISSING
)

_EMPTY: dict[type, object] = {str: "", bool: False, int: 0, list: [], dict: {}}

# Retained for the round-trip coupling test: every field the reader restores.
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
    ("config_schema_version", 0),
)


def backend_identity(agent_type: str, working_directory: str) -> str:
    """The identity a stored `session_id` is only valid within (§2.4).

    A state record names the *agent*, and recreation resolves whatever that name means
    now. Backend type and working directory together scope the backend's session store,
    so if either changed while the record sat idle, the stored id belongs to a different
    store: replaying it either loses continuity silently or matches an unrelated session
    that happens to carry the same id.

    The directory is **canonicalized**, and that is the whole point rather than tidiness:
    the config loader resolves relative paths but leaves an absolute one as written, and
    a backend subprocess launched with `cwd=/srv/current` reports the *physical* path
    from `getcwd()` (verified: retargeting the symlink changes what the child sees, and
    Claude Code's session store lives under a slugified physical path). So a deploy
    symlink repointed between restarts changes the store while leaving the configured
    string identical — the exact replay this comparison exists to catch, invisible to an
    uncanonicalized identity.

    An empty `working_directory` stays empty rather than resolving to the process cwd:
    config load requires the field and requires it to exist, so empty reaches here only
    from tests constructing `AgentConfig()` directly, and resolving it would make their
    identity depend on where pytest was invoked.

    `type:working_directory`, and the separator is load-bearing rather than cosmetic —
    the value is compared against records already on disk, so changing the spelling
    invalidates every stored identity and silently restarts every session. Takes the two
    values rather than an `AgentConfig` so `gateway.core.state` keeps importing nothing
    from the config layer.

    Deliberately not a digest: this one is read by operators in log lines, where "changed
    from claude:/srv/a to claude:/srv/b" is the whole message and two hashes would say
    nothing. It is not a filesystem key — those are in `gateway/core/paths.py` and are
    digests for the opposite reason.
    """
    resolved = str(Path(working_directory).resolve()) if working_directory else ""
    return f"{agent_type}:{resolved}"


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


def connector_name_of(path: Path) -> str:
    """`state.<name>.json` → `<name>`.

    Sliced rather than split: a connector name may contain dots, and only the leading
    `state.` and trailing `.json` belong to the file format. Written once because two
    callers need it and a second spelling would differ exactly on the names that make
    the rule worth stating.
    """
    return path.name[len("state."):-len(".json")]


class DuplicateSessionError(Exception):
    """Two persisted watchers claim one backend session for different rooms (§4.1)."""


def check_session_uniqueness() -> None:
    """Refuse to start when a state file binds one session to two rooms.

    The runtime check in `SessionMaps.bind_session` catches this too, but only when the
    second watcher gets that far — after the first has started answering, and with which
    watcher "wins" decided by start order. Reading the files first turns that into a
    refusal to boot with both records named.

    Reads **every** state file, not the connector being started: `SessionMaps` is one
    instance shared by all of them (`GatewayService` builds it once), so two connectors'
    records can collide with each other.

    Keyed by `session_id` alone, matching `SessionMaps`: every routing map there, and
    every consumer of them, uses the bare id, so two records claiming one id collide
    whichever backends issued them. Records with **no** identity are skipped rather than
    compared. That is not leniency: a record without one cannot have
    its session reused at all — `_provision_session` treats an unverifiable identity as
    a mismatch and starts fresh — so two such records never end up sharing a live
    session, and refusing them would reject a state that heals itself on the next start.

    Two records on the same room **of the same connector** are not a conflict here; that
    is a duplicate watcher, which the dispatcher refuses when the second claims the room.
    The connector is part of that comparison because `bind_session` compares it too: two
    records with one session id and one room id but different connectors bind different
    routing, and treating them as harmless here left the outcome to start order — one
    watcher running, the other reported as failed, differently on each boot.
    """
    seen: dict[str, tuple[str, str, str]] = {}
    for path in state_files():
        connector_name = connector_name_of(path)
        for record in load_state(connector_name):
            if not record.session_id or not record.backend_identity:
                continue
            previous = seen.get(record.session_id)
            if previous is None:
                seen[record.session_id] = (
                    record.watcher_name, record.room_id, connector_name)
                continue
            other_name, other_room, other_connector = previous
            if other_room == record.room_id and other_connector == connector_name:
                continue
            raise DuplicateSessionError(
                f"Watchers '{other_name}' (room '{other_room}' on connector "
                f"'{other_connector}') and '{record.watcher_name}' (room "
                f"'{record.room_id}' on connector '{connector_name}') both claim backend "
                f"session {record.session_id[:8]}. A session carries its room in its "
                f"transcript, its identity header and its permission routing, so one "
                f"session serving two rooms leaks each room's conversation into the "
                f"other. Clear the session_id on one of those records in "
                f"{RUNTIME_DIR} — the watcher will start a fresh session and keep "
                f"everything else it has."
            )


def check_state_formats() -> None:
    """Raise on the first state file this build cannot read.

    A preflight for callers that must not proceed past an unreadable file — the daemon
    on startup, and `config validate`. Reading each file is cheap next to the cost of
    the alternative: booting with an empty registry and abandoning every session while
    looking successful.
    """
    for path in state_files():
        load_state(connector_name_of(path))


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


class _MalformedRecord(ValueError):
    """One record is unusable. It is skipped with the field named, not fatal."""


def _record_from_dict(w: dict) -> WatcherState:
    """Build a WatcherState from one persisted record, checking every field's type.

    The reader validates rather than trusts, because a value read and used without
    checking it is the type the reader assumed is the defect shape this loader family
    keeps producing — and here it escaped the file layer entirely. A `watcher_name` of
    `[]` was accepted, and `StateStore.load()` then built `{ws.watcher_name: ws}` and
    raised `TypeError: unhashable type`, aborting startup: a crash in a caller, so no
    amount of care inside `load_state` could have caught it, and it contradicted the
    graceful-corruption contract stated in this module's own docstring.

    Types come from `_FIELD_TYPES`, derived from the dataclass's annotations, so a
    field added there is validated without anyone having to remember.
    """
    name = w.get("watcher_name")
    if not isinstance(name, str) or not name:
        raise _MalformedRecord(
            f"'watcher_name' must be a non-empty string (got {type(name).__name__})"
        )

    values: dict[str, object] = {}
    for field_name, want in _FIELD_TYPES.items():
        if field_name not in w:
            continue
        value = w[field_name]
        # bool before int: bool subclasses int, so an int field must reject True and a
        # bool field must not accept 0/1.
        if want is bool:
            ok = isinstance(value, bool)
        elif want is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, want)
        if not ok:
            raise _MalformedRecord(
                f"'{field_name}' must be {want.__name__} (got {type(value).__name__})"
            )
        # Copy the containers so a record never aliases the payload it came from.
        values[field_name] = (
            dict(value) if want is dict else list(value) if want is list else value
        )
    # A required field absent from the payload reads as empty, which is what the
    # previous reader did via its per-field defaults. Derived from `fields()` rather
    # than a hand-written list of "the required ones", and deliberately not solved by
    # giving those fields dataclass defaults: that would loosen a real constraint on
    # every construction site to serve the reader alone.
    for field_name in _REQUIRED_FIELDS - values.keys():
        values[field_name] = _EMPTY[_FIELD_TYPES[field_name]]
    return WatcherState(watcher_name=name, **values)


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

    raw_records = data.get("watchers", [])
    if not isinstance(raw_records, list):
        logger.warning(
            "[%s] State file's 'watchers' is not a list, starting fresh", connector_name
        )
        return []
    watchers = []
    for w in raw_records:
        if not isinstance(w, dict):
            logger.warning(
                "[%s] Skipping a non-mapping entry in the state file", connector_name
            )
            continue
        try:
            watchers.append(_record_from_dict(w))
        except (_MalformedRecord, TypeError, ValueError) as e:
            # One bad record is skipped rather than discarding the file: the others are
            # real sessions, and dropping them would abandon more than the corruption
            # did. The field is named so the operator can repair or delete by hand.
            logger.warning(
                "[%s] Skipping malformed state record: %s", connector_name, e
            )
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
