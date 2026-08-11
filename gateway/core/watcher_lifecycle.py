"""WatcherLifecycle: manages watcher start/stop/pause/resume/reset.

Extracted from SessionManager to keep watcher management logic focused.
Owns the _processors and _states dicts, delegates to MessageDispatcher,
InjectedContextBuilder, and StateStore for their respective concerns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from ..agents import AgentBackend
from .adapter_utils import ts_gt as _ts_gt
from .attachment_workspace import AttachmentWorkspace
from .config import CoreConfig, WatcherConfig, auto_watcher_name
from .connector import Connector
from .dispatch import MessageDispatcher
from .history_context import format_history_context
from .injected_context_builder import InjectedContextBuilder
from .message_processor import MessageProcessor
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import WatcherState
from .state_store import StateStore

logger = logging.getLogger("agent-chat-gateway.core.watcher_lifecycle")


class WatcherLifecycle:
    """Manages watcher start/stop/pause/resume/reset and related bookkeeping.

    Collaborators:
        - StateStore: persistence
        - MessageDispatcher: room→processor index
        - InjectedContextBuilder: context file build + durable delivery
        - SessionMaps: shared session routing state
    """

    def __init__(
        self,
        connector: Connector,
        agents: dict[str, AgentBackend],
        default_agent: str,
        config: CoreConfig,
        watcher_configs: list[WatcherConfig],
        state_store: StateStore,
        dispatcher: MessageDispatcher,
        injector: InjectedContextBuilder,
        permission_registry: PermissionRegistry | None,
        maps: SessionMaps,
        watcher_rules: "list[WatcherConfig] | None" = None,
        reserve_global_name: Callable[[str], Awaitable[bool]] | None = None,
        release_global_name: Callable[[str], None] | None = None,
    ) -> None:
        self._connector = connector
        self._agents = agents
        self._default_agent = default_agent
        self._config = config
        self._watcher_configs = watcher_configs
        self._state_store = state_store
        self._dispatcher = dispatcher
        self._injector = injector
        self._permission_registry = permission_registry
        self._maps = maps
        self._attachment_workspace = AttachmentWorkspace(connector)
        self._blocked_agents: set[str] = set()
        # room: "*" rule-based room-matching templates for this connector
        # (docs/design/on-the-fly-watchers.md) — already pre-filtered to
        # this connector by the caller, same convention as watcher_configs.
        # Optional (defaults to none) so every existing caller/test
        # constructing a WatcherLifecycle without lazy creation in mind
        # keeps working unchanged.
        self._watcher_rules: list[WatcherConfig] = watcher_rules or []
        # Cross-connector watcher-name reservation (PR #79 review, fourth
        # AND seventh rounds) — see GatewayService._reserve_watcher_name()
        # for why this can't be answered from inside a single connector's
        # own WatcherLifecycle. `reserve_global_name` must be an ATOMIC
        # check-and-reserve, not a plain read: each WatcherLifecycle only
        # ever locks by name WITHIN its own connector
        # (`_get_watcher_lock()`), so two different connectors racing to
        # lazily create a watcher for a name that collides across
        # connectors would otherwise both observe "available" before
        # either registers anything (seventh round finding). The caller
        # MUST pair a successful reservation with a `release_global_name`
        # call once the transient reservation is no longer needed (either
        # because the watcher is now durably registered in
        # `_watcher_configs`, or because creation was abandoned).
        # Both optional (default to "always available" / no-op release) so
        # every existing caller/test constructing a WatcherLifecycle
        # without a GatewayService above it keeps working unchanged — those
        # callers have no cross-connector name space to protect anyway.
        self._reserve_global_name = reserve_global_name or self._always_available
        self._release_global_name = release_global_name or (lambda _name: None)

        self._processors: dict[str, MessageProcessor] = {}
        self._states: dict[str, WatcherState] = {}
        # Per-watcher mutex: prevents concurrent pause/resume/reset commands for
        # the same watcher from racing through _stop_processor / _start_watcher.
        # The control socket can serve multiple simultaneous clients, so two
        # commands targeting the same watcher could otherwise interleave and
        # corrupt _processors / _states.
        self._watcher_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    async def _always_available(_name: str) -> bool:
        """Default `reserve_global_name` when no GatewayService is present."""
        return True

    # ── Sync ──────────────────────────────────────────────────────────────────

    def seed_blocked_agents(self, unavailable_agents: set[str] | None) -> None:
        """Set `self._blocked_agents` ahead of enabling live message
        callbacks — call this BEFORE `connector.connect()`, not after.

        PR #79 review, eleventh round, finding #27: `SessionManager.run_once()`
        registers `try_lazy_create()` as the lazy-creation hook and awaits
        `connector.connect()` (which starts the Mattermost websocket
        listener as a background task) *before* calling `sync_watchers()` —
        and `sync_watchers()` is the only place that previously populated
        `self._blocked_agents` (still the empty set from `__init__` until
        then). A message arriving in that window would pass
        `try_lazy_create()`'s fail-closed `agent_name in self._blocked_agents`
        check even for an agent whose permission broker genuinely failed to
        start, lazily creating a watcher with zero tool-call enforcement —
        the exact hole `sync_watchers()`'s own identical check (and the
        seventh review round's fix extending it to the lazy path) was
        built to close, just reopened by startup ordering.

        Idempotent with `sync_watchers()`'s own identical assignment
        (which stays unchanged, including its None-means-"don't touch"
        semantics for a hypothetical hot-reload re-call) — calling both is
        deliberate, not a bug: this seeds the value early enough to matter
        for the pre-`sync_watchers()` window; `sync_watchers()` then
        harmlessly re-applies the same value once it runs.
        """
        if unavailable_agents is not None:
            self._blocked_agents = set(unavailable_agents)

    async def sync_watchers(
        self, unavailable_agents: set[str] | None = None
    ) -> list[str]:
        """Start processors for all active (non-paused) watchers defined in config.

        Args:
            unavailable_agents: Optional set of agent names that are unavailable
                (backend failed to start, or permission broker failed to start).
                Any watcher whose resolved agent is in this set is skipped rather
                than started without broker enforcement — starting without a broker
                would silently bypass tool-call permission checks.

        Returns:
            List of human-readable error strings for any watchers that failed.
        """
        errors: list[str] = []
        persisted = self._state_store.load()
        blocked_agents = unavailable_agents or set()
        # Only update _blocked_agents when the caller explicitly provides the
        # set of unavailable agents.  Passing None (the default) means "no
        # information about agent availability" — NOT "all agents are available".
        # Unconditionally overwriting with an empty set when None is passed would
        # silently disarm the fail-closed _ensure_agent_available guard in
        # resume_watcher / reset_watcher, allowing watchers to start without
        # their permission brokers if sync_watchers is ever called a second time
        # without an availability check (e.g., a hot-reload path).
        if unavailable_agents is not None:
            self._blocked_agents = set(blocked_agents)

        # PR #79 review (sixth round): snapshot, NOT a live reference — the
        # websocket listen loop is already running concurrently by this
        # point (SessionManager.run_once() calls connector.connect(), which
        # starts it as a background task, BEFORE calling sync_watchers()).
        # If a Mattermost event completes try_lazy_create() while this loop
        # is still awaiting an earlier static watcher's own _start_watcher()
        # call, that lazy path's `self._watcher_configs.append(wc)` would
        # otherwise mutate the exact list object this `for` loop is
        # iterating — Python's list iterator picks up appended items during
        # iteration, so this loop would eventually revisit that
        # already-fully-started lazy watcher and call _start_watcher() on
        # it a SECOND time: _processors[name] gets overwritten with a new
        # MessageProcessor, but the FIRST one is never stopped or
        # unregistered — MessageDispatcher.add_processor() appends rather
        # than replaces, so it stays registered too, leaving TWO live
        # processors for the same room (duplicate agent responses) and an
        # orphaned, untracked first processor (no longer reachable via
        # _processors, never stoppable again). A plain list() snapshot
        # here is immune: nothing appended to the real
        # self._watcher_configs during iteration is ever visited by this
        # loop, which is exactly what's wanted — the lazy path already
        # fully started that watcher itself; there's nothing left for this
        # loop to redundantly do for it anyway.
        for wc in list(self._watcher_configs):
            state = persisted.get(wc.name)
            if state and state.paused:
                logger.info("Watcher '%s' is paused — skipping startup", wc.name)
                self._states[wc.name] = state
                continue

            # Fail-closed: refuse to start a watcher whose agent's permission
            # broker could not be initialized.  A watcher that starts without
            # its broker would process messages with no tool-call enforcement.
            agent_name = self._resolve_agent_name(wc.agent)
            if agent_name in blocked_agents:
                msg = (
                    f"Watcher '{wc.name}' (room '{wc.room}'): skipped — "
                    f"agent '{agent_name}' is unavailable "
                    f"(backend or permission broker failed to start)"
                )
                logger.error(msg)
                errors.append(msg)
                continue

            try:
                # Hold the per-watcher lock for the entire _start_watcher call
                # so that a concurrent pause/resume/reset command (arriving via
                # the control socket after the socket is opened) cannot interleave
                # with the subscribe window.  Without the lock, a pause_watcher()
                # call arriving while _start_watcher is blocked at
                # subscribe_room() would find the processor already in
                # _processors, call stop() on it (sets state="stopped"), and then
                # _start_watcher would resume to call processor.start() — leaving
                # the processor in "stopped" state with a running consumer task,
                # silently dropping every subsequent message.
                # All other _start_watcher callers (resume_watcher, reset_watcher)
                # already hold this lock, so this makes the invariant uniform.
                async with self._get_watcher_lock(wc.name):
                    # Seed self._states from the disk-persisted copy read
                    # above before calling — _start_watcher() now reads its
                    # own state from self._states rather than taking it as
                    # a parameter (PR #79 review, fifteenth round; see its
                    # docstring). Done here, under the lock, immediately
                    # before the call.
                    if state is not None:
                        self._states[wc.name] = state
                    await self._start_watcher(wc)
            except Exception as e:
                msg = f"Watcher '{wc.name}' (room '{wc.room}'): failed to start: {e}"
                logger.error(msg)
                errors.append(msg)

        # Note: state entries for watchers removed from config are not actively
        # deleted from the persisted file here.  The next save() call (line below)
        # only persists self._states, which only contains watchers that were
        # started or are paused in this run — removed watchers are implicitly
        # dropped from the next save.  Log at debug level to avoid misleading
        # "pruning" messages when no actual deletion is performed yet.
        config_names = {wc.name for wc in self._watcher_configs}
        for name in list(persisted):
            if name not in config_names:
                if (
                    persisted[name].dynamically_created
                    and self._watcher_rules
                    and await self._dynamic_state_still_matches_rule(persisted[name])
                ):
                    # PR #79 review finding: a lazily-created watcher
                    # (docs/design/on-the-fly-watchers.md) is NEVER in
                    # `_watcher_configs` at boot — its WatcherConfig only
                    # ever lived in memory during the run that created it.
                    # Without this, the save() below (which only persists
                    # self._states) would silently drop its entry on the
                    # very first restart, breaking "resume a dormant
                    # session" after exactly one restart. Carried forward
                    # as-is (not started/pre-warmed here — that's a
                    # separate, not-yet-built optimization, see the design
                    # doc) so the NEXT message for that room can still find
                    # and resume it via try_lazy_create().
                    #
                    # Gated on `self._watcher_rules` being non-empty (PR
                    # #79 review, tenth round, finding #25): if the
                    # operator has since removed this connector's `room:
                    # "*"` rule entirely, `_reconstruct_dynamic_watcher_config()`
                    # explicitly refuses to bring this watcher back
                    # (nothing to match against) — preserving it here
                    # anyway would keep it (and the disk write below) alive
                    # forever with zero path back, AND `is_watcher_name_known()`
                    # would keep reporting its name as globally taken
                    # forever, permanently blocking any OTHER connector
                    # from ever claiming that generated name for a
                    # watcher that can actually run.
                    #
                    # ALSO gated on `_dynamic_state_still_matches_rule()`
                    # (PR #79 review, eleventh round, finding #28): a rule
                    # existing at all isn't enough — if the operator added
                    # this exact room to `exclude_room:` since this watcher
                    # was created, every future message for it is rejected
                    # by try_lazy_create()'s own exclusion check AND
                    # `_reconstruct_dynamic_watcher_config()` refuses it
                    # too, so it's just as unreachable as the rule-removed
                    # case above — just a narrower trigger.
                    self._states[name] = persisted[name]
                elif persisted[name].dynamically_created:
                    logger.info(
                        "Watcher '%s' was dynamically created but this "
                        "connector no longer has an active, matching "
                        "wildcard rule for its room — pruning its "
                        "persisted state (it can never be reconstructed).",
                        name,
                    )
                else:
                    logger.debug(
                        "Watcher '%s' not in current config — will be omitted from next state save",
                        name,
                    )

        self._state_store.save(self._states)
        return errors

    async def _dynamic_state_still_matches_rule(self, state: WatcherState) -> bool:
        """True if `state`'s room still resolves and is not excluded by
        this connector's active wildcard rule.

        Used by `sync_watchers()`'s startup preservation loop (PR #79
        review, eleventh round, finding #28) — preserving a dynamic
        watcher's state just because SOME rule exists (the tenth round's
        finding #25 fix) isn't enough: if the operator added this exact
        room to `exclude_room:` since the watcher was created, it's just
        as permanently unreachable as if the whole rule had been removed,
        yet nothing pruned it.

        Fast path: skips the network round-trip entirely when the active
        rule has no `exclude_rooms` at all (the common case) — nothing
        could possibly exclude this room in that case.

        Resolution failure preserves conservatively (True) rather than
        pruning — consistent with `_reconstruct_dynamic_watcher_config()`'s
        own best-effort error handling: a transient network blip during
        startup must not permanently discard a legitimate session.
        """
        rule = self._watcher_rules[0]
        if not rule.exclude_rooms:
            return True
        try:
            room = await self._connector.resolve_room_by_id(state.room_id)
        except Exception as e:
            logger.warning(
                "sync_watchers: could not resolve room id=%s while checking "
                "dynamic watcher '%s' against exclude_room: — preserving "
                "conservatively: %s", state.room_id, state.watcher_name, e,
            )
            return True
        return room.name not in rule.exclude_rooms

    def _get_watcher_lock(self, name: str) -> asyncio.Lock:
        """Return (creating if needed) the per-watcher mutex for lifecycle ops."""
        if name not in self._watcher_locks:
            self._watcher_locks[name] = asyncio.Lock()
        return self._watcher_locks[name]

    # ── Lazy (rule-matched) watcher creation ─────────────────────────────────
    # docs/design/on-the-fly-watchers.md. Called by a connector when a
    # message arrives for a room with no existing watcher/state — see
    # MattermostConnector._on_posted_event's hook. RC has no equivalent
    # caller yet (see the design doc's "trigger point differs by connector"
    # section) — this method itself is connector-agnostic and will be
    # reused once RC's own event-hook-triggered path is built.

    async def try_lazy_create(self, room_id: str) -> bool:
        """Resolve `room_id`, check it against this connector's watcher_rules,
        and — if exactly one rule matches — create a full watcher for it on
        the spot (subscribe, session, processor; the same `_start_watcher()`
        every static watcher goes through).

        Returns True if a watcher now exists for this room (either just
        created here, or already existing — e.g. two near-simultaneous
        callers) and the caller should proceed with processing the
        triggering message. Returns False if no rule matches (or resolution/
        creation failed) and the caller should keep dropping messages for
        this room, exactly as before this feature existed.

        Never raises — a resolution or creation failure is logged and
        treated as "no match", not propagated to the connector's message-
        handling loop.
        """
        if not self._watcher_rules:
            return False

        try:
            room = await self._connector.resolve_room_by_id(room_id)
        except Exception as e:
            logger.warning(
                "try_lazy_create: failed to resolve room id=%s: %s", room_id, e
            )
            return False

        # Wildcard rules match channels only, never DMs — binding an agent
        # to every DM the bot account has is very unlikely to be the intent
        # of "listen to all rooms I have access to" (docs/design/
        # on-the-fly-watchers.md, decided 2026-08-05).
        if room.type == "dm":
            return False

        # At most one rule per connector is enforced at config-load time
        # (gateway/config.py) — never ambiguous which rule's agent to use.
        rule = self._watcher_rules[0]
        if room.name in rule.exclude_rooms:
            return False

        # Fail-closed, same posture as sync_watchers()'s identical check
        # before every static watcher start (PR #79 review): an agent whose
        # backend or permission broker failed at startup must never get a
        # watcher started for it via ANY path, lazy or static — otherwise
        # tool calls in a lazily-created watcher would run with zero
        # enforcement.
        agent_name = self._resolve_agent_name(rule.agent)
        if agent_name in self._blocked_agents:
            logger.warning(
                "try_lazy_create: agent '%s' is unavailable (backend or "
                "permission broker failed to start) — refusing to lazily "
                "create a watcher for room '%s'", agent_name, room.name,
            )
            return False

        watcher_name = auto_watcher_name(rule.connector, room.name)

        async with self._get_watcher_lock(watcher_name):
            # PR #79 review (fourth round): check by ROOM first, across ALL
            # of this connector's watcher configs — not just a name lookup
            # for the auto-generated name. A static watcher can (and often
            # does) have an explicit custom `name:` for this exact room —
            # `get_watcher_config(watcher_name)` alone would never find it,
            # so a paused/stopped explicitly-named static watcher for this
            # room could otherwise get silently duplicated by a
            # differently-named lazy watcher for the same room.
            existing_for_room = next(
                (wc for wc in self._watcher_configs if wc.room == room.name), None
            )
            if existing_for_room is None:
                # PR #79 review (fifth round): `wc.room` is a NAME, which
                # is not stable — a channel renamed on the platform while
                # its watcher is paused means the just-resolved room.name
                # no longer matches the config's stored (stale) name, even
                # though it's the exact same room. Fall back to matching
                # by the room's STABLE platform ID via each watcher's
                # state (state.room_id is set from the real resolved room
                # every time _start_watcher runs, and — since the fifth-
                # round fix just above — now survives resume/reset too).
                # Empty room_id (a watcher paused via the CLI's own
                # not-found fallback, before ever actually starting) is
                # deliberately excluded — nothing to confirm a match against.
                state_for_room_id = next(
                    (s for s in self._states.values() if s.room_id and s.room_id == room.id),
                    None,
                )
                if state_for_room_id is None:
                    # PR #79 review (tenth round, finding #24): self._states
                    # alone can miss a dormant dynamic watcher during
                    # startup. `SessionManager.run_once()` calls
                    # `connector.connect()` (which starts the Mattermost
                    # websocket listener as a background task) BEFORE
                    # `sync_watchers()`, and `sync_watchers()` only copies a
                    # dormant dynamic watcher's persisted state into
                    # `self._states` as its VERY LAST step (after every
                    # static watcher has already started). A message for a
                    # RENAMED dynamic watcher's room arriving anywhere in
                    # that window would find `self._states` empty or
                    # incomplete, miss this fallback entirely, and then
                    # miss the "resume a dormant session" step below too
                    # (which looks up disk state by the NEW, post-rename
                    # name — the persisted entry is still under the OLD
                    # name) — silently creating a brand-new watcher/session
                    # under the new name, abandoning the old one and
                    # bypassing a persisted pause. Falls back to a direct
                    # disk read, which is always complete/authoritative
                    # this early in the process's life (nothing has been
                    # mutated in memory yet that isn't already on disk) —
                    # the "resume a dormant session" step below already
                    # reads disk directly for the identical reason.
                    state_for_room_id = next(
                        (
                            s for s in self._state_store.load().values()
                            if s.room_id and s.room_id == room.id
                        ),
                        None,
                    )
                if state_for_room_id is not None:
                    existing_for_room = self.get_watcher_config(state_for_room_id.watcher_name)
                    if (
                        existing_for_room is not None
                        and state_for_room_id.dynamically_created
                        and existing_for_room.room != room.name
                    ):
                        # PR #79 review: `existing_for_room` here is a
                        # CACHED WatcherConfig from `_watcher_configs`,
                        # already reconstructed earlier in this same
                        # process (e.g. by a prior pause/resume/reset call
                        # or an earlier try_lazy_create() for this room) —
                        # NOT freshly built from the just-resolved `room`.
                        # If the room was renamed AFTER that earlier
                        # reconstruction, this cached config's `.room` is
                        # now stale: the room-name check just above
                        # (`wc.room == room.name`) already missed it for
                        # that exact reason, which is how execution reached
                        # this room-ID fallback branch at all. Left
                        # unrefreshed, a later `resume`/`reset` on this
                        # watcher would call `_start_watcher()` ->
                        # `resolve_room(wc.room)` with the OLD name, which
                        # no longer exists — resume/reset would fail until
                        # a full gateway restart forces `_watcher_configs`
                        # to be rebuilt from scratch. Gated on
                        # `dynamically_created` so this can only ever touch
                        # a lazily-created config, never an operator's
                        # static one (whose `.room` is config.yaml's
                        # explicit, intentional value, not a cache).
                        logger.info(
                            "try_lazy_create: room for dynamic watcher '%s' "
                            "was renamed ('%s' -> '%s') since it was last "
                            "reconstructed in this process — refreshing the "
                            "cached config so a future resume/reset resolves "
                            "the current name.",
                            existing_for_room.name, existing_for_room.room, room.name,
                        )
                        existing_for_room.room = room.name
                    if (
                        existing_for_room is None
                        and state_for_room_id.watcher_name != watcher_name
                    ):
                        # PR #79 review (seventh round, finding #21): a
                        # dynamic watcher's persisted state can outlive its
                        # WatcherConfig across a restart — get_watcher_config()
                        # above just returned None because it was never
                        # reconstructed, NOT because there's nothing to find.
                        # `state_for_room_id.watcher_name != watcher_name`
                        # (computed from room.name a few lines above) means
                        # the room was RENAMED since this watcher was
                        # created — silently proceeding to build a
                        # brand-new config under the new auto-generated
                        # name would abandon the old session and bypass a
                        # persisted pause entirely (the "resume a dormant
                        # session" step below looks up state by the NEW
                        # name, which has no persisted entry). Reconstruct
                        # the OLD watcher so the block below recognizes it
                        # as an existing, dormant watcher needing explicit
                        # `resume` — same treatment any other paused/stopped
                        # watcher for this room gets.
                        #
                        # Deliberately NOT done when the name is unchanged
                        # (no rename): that's just the ordinary "next
                        # message after a restart" case for an un-paused
                        # dynamic watcher, and the "resume a dormant
                        # session" step further below (which loads state by
                        # this exact, unchanged name) is what's SUPPOSED to
                        # bring it back — reconstructing and blocking here
                        # too would leave it stuck dormant forever instead
                        # (finding #20, a separate seventh-round bug fixed
                        # near the reservation call below).
                        existing_for_room = await self._reconstruct_dynamic_watcher_config(
                            state_for_room_id.watcher_name
                        )
            if existing_for_room is not None:
                if existing_for_room.name in self._processors:
                    # Already running (under its own name, custom or
                    # auto-generated) — either a concurrent caller for this
                    # exact room won the race while we waited for the lock,
                    # or it was already live before this call.
                    return True
                # A WatcherConfig for this exact room already exists (static
                # or previously lazily-created) but has no running
                # processor — paused, stopped, or never successfully
                # started. Lazy creation must NEVER implicitly (re)start an
                # already-known watcher for this room — that would silently
                # override an explicit pause, or race a legitimate
                # pause/resume/reset call (which holds this same per-name
                # lock). Only pause_watcher()/resume_watcher()/
                # reset_watcher() may bring it back to life.
                return False

            # Existing-BY-NAME check deliberately lives INSIDE the lock too,
            # not before it: two DIFFERENT rooms that sanitize to the SAME
            # auto-generated name (e.g. differing only in punctuation
            # stripped by sanitize_room_for_name()) arrive on Mattermost via
            # two INDEPENDENT per-channel worker queues (websocket.py's
            # _dispatch()) — they are not naturally serialized against each
            # other the way two messages for the SAME room are. Checking
            # before the lock would let both callers race past a "no
            # collision yet" read before either had registered anything.
            # (existing_for_room above already ruled out this being the
            # SAME room, so any match here is necessarily a different one.)
            existing_by_name = self.get_watcher_config(watcher_name)
            if existing_by_name is not None:
                logger.error(
                    "try_lazy_create: auto-generated name '%s' for room "
                    "'%s' collides with an existing watcher for a "
                    "different room ('%s') — refusing to create.",
                    watcher_name, room.name, existing_by_name.room,
                )
                return False

            # Resume a dormant session from a prior run, if one was
            # persisted under this exact (deterministic) name — same
            # resume-first behavior _provision_session() already gives
            # every static watcher.
            state = self._state_store.load().get(watcher_name)

            reserved = False
            try:
                # Cross-connector RESERVATION (PR #79 review, fourth,
                # seventh AND ninth rounds): the two checks above only see
                # THIS connector's own watchers. Watcher names are assumed
                # globally unique across every connector
                # (ControlServer._find_entry_for_watcher(), the scheduler)
                # — an invariant already enforced for static watchers at
                # config-load time, but never checked for a name generated
                # at runtime. A plain read-then-create here would still
                # race: two connectors each hold only their OWN per-name
                # lock (`_get_watcher_lock()`), so two near-simultaneous
                # `try_lazy_create()` calls on DIFFERENT connectors whose
                # room/connector pairs sanitize to the same name could both
                # observe "available" before either registers anything
                # (seventh round finding). `reserve_global_name` must
                # therefore perform an atomic check-and-reserve; every exit
                # past this point where a reservation was actually taken —
                # success or failure — releases it via the `finally` block
                # below.
                #
                # Called UNCONDITIONALLY, including when this connector's
                # own persisted state already owns this exact name for this
                # exact room (a plain "next message after a restart"
                # self-reclaim, no rename) — the eighth round's fix instead
                # skipped this call entirely for that case, which (per the
                # ninth round's finding #22) let a DIFFERENT connector's
                # meanwhile-configured static watcher silently claim the
                # very same name unchecked, since dormant dynamic state was
                # never visible to that other connector's own config-load-
                # time uniqueness check. The self-block bug the eighth
                # round was fixing is instead solved on the GatewayService
                # side (`_reserve_watcher_name()`): the reservation check
                # excludes the REQUESTING connector's own entry from the
                # "does anyone already have this" scan (this connector's
                # own existing_by_name/existing_for_room checks above
                # already covered that), while still checking every OTHER
                # connector — so a foreign collision is still caught, and a
                # true self-reclaim no longer reports itself as occupied.
                # That same GatewayService-side check also no longer
                # reconstructs a colliding OTHER connector's dormant config
                # as a side effect of merely probing it (ninth round,
                # finding #23) — see `is_watcher_name_known()`.
                if not await self._reserve_global_name(watcher_name):
                    logger.error(
                        "try_lazy_create: auto-generated name '%s' for room "
                        "'%s' is already used by a watcher on a different "
                        "connector — refusing to create (watcher names must "
                        "be globally unique).",
                        watcher_name, room.name,
                    )
                    return False
                reserved = True

                wc = self._build_watcher_config_from_rule(watcher_name, rule, room.name)

                # PR #79 review (fourth round): the persisted state under this
                # NAME might belong to a DIFFERENT room that happened to
                # sanitize to the same name in a PRIOR run (e.g. one whose
                # watcher was never re-created after that run ended) — reusing
                # its session_id here would bind the WRONG room's conversation
                # history/context onto this room, violating the 1-session-per-
                # room invariant this whole feature is built around. Checked by
                # room_id, not name, since name is exactly what's ambiguous here.
                if state is not None and state.room_id and state.room_id != room.id:
                    logger.error(
                        "try_lazy_create: persisted state for '%s' belongs to a "
                        "different room (room_id=%s) than the one just resolved "
                        "(room_id=%s, name=%s) — likely two rooms colliding on "
                        "the same auto-generated name; refusing to reuse or "
                        "overwrite it.",
                        watcher_name, state.room_id, room.id, room.name,
                    )
                    return False

                # PR #79 review (second round): after a restart, this room's
                # WatcherConfig is gone from `_watcher_configs` (never
                # persisted — only the runtime state is, via
                # dynamically_created), so the `existing_for_room` check above
                # can't see it and never fires. Without this second check here,
                # a paused lazy watcher's persisted state (paused=True) would
                # get resumed by the very next message post-restart, with no
                # explicit `resume` — _start_watcher() itself has no
                # "is this paused" gate, it always starts unconditionally.
                if state is not None and state.paused:
                    logger.info(
                        "try_lazy_create: room '%s' has a paused persisted "
                        "watcher ('%s') — not auto-resuming; use "
                        "'agent-chat-gateway resume %s' to bring it back.",
                        room.name, watcher_name, watcher_name,
                    )
                    return False

                try:
                    # Seed self._states from the disk-loaded copy read above
                    # — _start_watcher() now reads its own state from
                    # self._states rather than taking it as a parameter (PR
                    # #79 review, fifteenth round; see its docstring).
                    # Deliberately done here, AFTER the room-id-mismatch and
                    # paused refusals above (both return False without
                    # reaching this point) — seeding any earlier would leave
                    # a stray self._states entry behind on those refusal
                    # paths, which save() below would then persist for a
                    # watcher that was never actually started.
                    if state is not None:
                        self._states[watcher_name] = state
                    await self._start_watcher(wc)
                except Exception as e:
                    logger.error(
                        "try_lazy_create: failed to start watcher '%s' for "
                        "room '%s': %s", watcher_name, room.name, e,
                    )
                    return False

                self._watcher_configs.append(wc)
                # Flag so sync_watchers() preserves this entry across a restart
                # even though `wc` only ever lives in memory, never in
                # config.yaml (PR #79 review — see WatcherState.dynamically_created).
                if watcher_name in self._states:
                    self._states[watcher_name].dynamically_created = True
                self._state_store.save(self._states)
                logger.info(
                    "Lazily created watcher '%s' for room '%s' (rule-matched, "
                    "agent '%s')", watcher_name, room.name, rule.agent,
                )
                return True
            finally:
                # Release the transient reservation regardless of outcome —
                # but ONLY if this call actually took one (`reserved`,
                # PR #79 review, seventh round finding #20's companion fix:
                # a self-reclaim never reserves in the first place, so
                # unconditionally releasing here would risk releasing a
                # DIFFERENT, unrelated caller's legitimate in-flight
                # reservation for the same name). On success the name is
                # now durably visible via `_watcher_configs` anyway (no
                # reservation needed to prove ownership going forward); on
                # any failure path after a real reservation, the name was
                # never claimed and must not be left stuck "reserved"
                # forever.
                if reserved:
                    self._release_global_name(watcher_name)

    # ── Lifecycle controls ────────────────────────────────────────────────────

    async def pause_watcher(self, name: str) -> None:
        """Pause a watcher: stop processing messages but preserve state."""
        await self._find_or_reconstruct_watcher_config(name)
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if state and state.paused:
                logger.info("Watcher '%s' is already paused", name)
                return
            try:
                await self._stop_processor(name, save=False)
            except Exception as e:
                # Best-effort teardown: _stop_processor already removed the processor
                # from _processors even when it raises (e.g. network error during
                # DDP unsubscribe).  Log the error but continue — marking the watcher
                # paused is still correct since it is no longer processing messages.
                logger.warning(
                    "Watcher '%s': error during stop phase of pause (proceeding with pause): %s",
                    name,
                    e,
                )
            if state:
                state.paused = True
            else:
                self._states[name] = WatcherState(
                    watcher_name=name,
                    session_id="",
                    room_id="",
                    paused=True,
                )
            self._state_store.save(self._states)
            logger.info("Watcher '%s' paused", name)

    async def resume_watcher(self, name: str) -> None:
        """Resume a paused watcher."""
        wc = await self._find_or_reconstruct_watcher_config(name)
        self._ensure_agent_available(wc)
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if name in self._processors:
                logger.info("Watcher '%s' is already running", name)
                # Clear paused flag and persist — the watcher is already running
                # so no restart is needed, but the flag must be updated.
                if state:
                    state.paused = False
                self._state_store.save(self._states)
                return
            try:
                await self._start_watcher(wc)
            except Exception as e:
                logger.error("Failed to resume watcher '%s': %s", name, e)
                raise
            # No explicit "clear paused" step needed here on success:
            # _start_watcher() always builds a brand-new WatcherState with
            # paused=False (it has no "resume vs fresh start" distinction —
            # every start is unpaused by construction), which is already
            # sitting in self._states[name] by the time we get here. The
            # local `state` reference above is the PRE-start object, which
            # _start_watcher() has since replaced — mutating it here would
            # touch a discarded WatcherState, not the current one.
            self._state_store.save(self._states)
            logger.info("Watcher '%s' resumed", name)

    async def reset_watcher(self, name: str) -> None:
        """Reset a watcher: clear session and restart with fresh state."""
        wc = await self._find_or_reconstruct_watcher_config(name)
        self._ensure_agent_available(wc)
        async with self._get_watcher_lock(name):
            try:
                await self._stop_processor(name, save=False)
            except Exception as e:
                # Best-effort teardown: log the error but continue with the restart.
                # A failure here (e.g. network error while sending DDP unsub) should
                # not prevent the user from recovering the watcher via reset.
                logger.warning(
                    "Watcher '%s': error during stop phase of reset (proceeding with restart): %s",
                    name,
                    e,
                )

            state = self._states.get(name)
            # Clear injection retry state BEFORE resetting context_injected so
            # the new startup attempt begins with a fresh failure counter.
            # Without this, a watcher that reached ``failed_degraded`` would
            # immediately re-enter that state after reset (the old failure_count
            # is still at ``_MAX_INJECT_ATTEMPTS``, so one more failure tips it
            # over again).
            # NOTE: computed OUTSIDE the `if state:` guard — a pinned wc.session_id
            # must be reset even when state is None (watcher failed before any
            # state was persisted).
            old_session_id = wc.session_id or (state.session_id if state else "")
            if old_session_id:
                self._injector.reset_session(old_session_id)
            if state:
                # Mutated in place — `state` is the same object as
                # self._states[name] (not a copy), so this is exactly the
                # "seed self._states before calling" step _start_watcher()
                # now requires of every caller whose prior state didn't
                # already live there untouched (PR #79 review, fifteenth
                # round; see _start_watcher()'s docstring). No separate
                # seed line needed here — the mutation itself is the seed.
                if not wc.session_id:
                    state.session_id = ""
                state.context_injected = False
                state.paused = False

            try:
                await self._start_watcher(wc)
            except Exception as e:
                logger.error("Failed to restart watcher '%s' after reset: %s", name, e)
                raise
            self._state_store.save(self._states)
            logger.info("Watcher '%s' reset", name)

    def list_watchers(self) -> list[dict]:
        """Return info for all configured watchers, including runtime status."""
        result = []
        for wc in self._watcher_configs:
            state = self._states.get(wc.name)
            processor = self._processors.get(wc.name)
            effective_session = wc.session_id or (state.session_id if state else "")
            result.append(
                {
                    "watcher_name": wc.name,
                    "room_name": wc.room,
                    "connector": wc.connector,
                    "agent_name": wc.agent,
                    "session_id": effective_session,
                    "paused": state.paused if state else False,
                    "active": processor is not None,
                    "context_injection_state": (
                        self._injector.status_for(effective_session).state
                        if effective_session
                        else "not_started"
                    ),
                }
            )
        return result

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._states.get(name)

    def get_processor(self, watcher_name: str) -> "MessageProcessor | None":
        """Return the active MessageProcessor for a watcher, or None if not running.

        Used by the scheduler to inject synthetic messages directly into the
        processing queue, bypassing the connector layer entirely (and therefore
        the self-message filter that would drop messages sent by the bot user).
        """
        return self._processors.get(watcher_name)

    async def wake_dormant_watcher(self, name: str) -> bool:
        """Best-effort auto-wake for a dormant, unpaused, dynamically-
        created watcher (PR #79 review).

        Used by the job scheduler before giving up on a scheduled-job
        delivery: a lazily created watcher intentionally goes dormant
        (config gone from `_watcher_configs`, no processor) between
        messages — that's normal, not a failure — but `inject_message()`
        previously just returned False the moment no processor existed,
        so every due/catch-up run for an otherwise-healthy dynamic watcher
        was silently skipped (and its next run advanced, per
        `_fire_once()`'s own anti-flood design) until unrelated channel
        traffic happened to wake it via `try_lazy_create()`.

        Deliberately narrow, mirroring resume_watcher()'s own guards
        exactly rather than a looser "just start it" attempt:
          - Only wakes a watcher already flagged `dynamically_created`.
            A static watcher that failed to start for some other reason
            (blocked agent, a startup error) is a different, unrelated
            failure mode this method must not paper over.
          - Never wakes a PAUSED watcher (static or dynamic) — the
            standing rule elsewhere in this file ("only pause_watcher()/
            resume_watcher()/reset_watcher() may bring it back to life")
            applies here too; a scheduled job for a paused watcher should
            keep silently skipping, exactly as before.
          - Respects the fail-closed blocked-agents guard
            (`_ensure_agent_available()`) — a scheduled job must not be
            able to force-start a watcher whose agent's permission broker
            failed, any more than a live message can.
          - Cross-connector reservation via `_reserve_global_name()`,
            same rationale as `try_lazy_create()`'s own (PR #79 review,
            fourth/seventh/ninth rounds): a dormant dynamic watcher's name
            is invisible to config-load-time uniqueness checks (it's never
            in config.yaml), so a static watcher configured on a DIFFERENT
            connector after this one went dormant could already be live
            under this exact name by the time a scheduled job wakes it —
            without reserving first, both would end up as live processors
            sharing a supposedly globally-unique name, breaking every
            piece of routing that assumes one owner per name.

        Returns True if the watcher is now running (already was, or was
        successfully started here); False if it doesn't exist, is paused,
        isn't a dynamic watcher, the name is claimed by a different
        connector, or it failed to start.
        """
        if name in self._processors:
            return True
        state = self._states.get(name)
        if state is None or state.paused or not state.dynamically_created:
            return False
        async with self._get_watcher_lock(name):
            if name in self._processors:
                return True
            # Re-fetch under the lock rather than trusting the snapshot
            # taken above (PR #79 review, fourteenth round): a concurrent
            # pause_watcher() call could have acquired this same lock
            # first and marked the watcher paused (or, if `state` was
            # None then, allocated a fresh paused WatcherState) between
            # our pre-lock snapshot and here. Without this recheck,
            # _start_watcher() below would construct a replacement state
            # with paused=False, silently undoing a pause that had
            # already completed.
            state = self._states.get(name)
            if state is None or state.paused or not state.dynamically_created:
                return False
            reserved = False
            try:
                if not await self._reserve_global_name(name):
                    logger.warning(
                        "wake_dormant_watcher: name '%s' is already used "
                        "by a watcher on a different connector — refusing "
                        "to wake (watcher names must be globally unique).",
                        name,
                    )
                    return False
                reserved = True
                wc = await self._find_or_reconstruct_watcher_config(name)
                self._ensure_agent_available(wc)
                # No seed needed: `state` (re-fetched from self._states just
                # above, under this same lock) is already the exact object
                # sitting in self._states[name] — _start_watcher() now reads
                # it from there itself (PR #79 review, fifteenth round).
                # Note _find_or_reconstruct_watcher_config() above may have
                # since replaced that entry with a fresh disk-loaded copy
                # (if this watcher's config wasn't already known); reading
                # self._states fresh inside _start_watcher(), rather than
                # reusing this now-possibly-superseded local `state`, is the
                # more correct choice, not just an equivalent one.
                await self._start_watcher(wc)
            except Exception as e:
                logger.warning(
                    "wake_dormant_watcher: failed to auto-start dormant "
                    "watcher '%s' for scheduled delivery: %s", name, e,
                )
                return False
            finally:
                # Same release discipline as try_lazy_create(): on success
                # the name is now durably visible via `_watcher_configs`/
                # `_processors` (no reservation needed to prove ownership
                # going forward); on any failure after a real reservation,
                # it must not be left stuck "reserved" forever.
                if reserved:
                    self._release_global_name(name)
            return True

    def get_watcher_config(self, watcher_name: str) -> "WatcherConfig | None":
        """Return the WatcherConfig for a watcher name, or None if not found."""
        return next((wc for wc in self._watcher_configs if wc.name == watcher_name), None)

    def is_watcher_name_known(self, name: str) -> bool:
        """Non-mutating: True if `name` is either a live WatcherConfig
        (static, or a dynamic one already reconstructed/started) or a
        persisted dynamically-created WatcherState — WITHOUT reconstructing
        or registering anything as a side effect.

        PR #79 review, ninth round, finding #23: `can_find_or_reconstruct_watcher()`
        is the wrong tool for a pure cross-connector availability PROBE
        (`GatewayService._reserve_watcher_name()`) — its reconstruction
        side effect (deliberate and correct for `ControlServer`, which
        immediately acts on a positive result) would otherwise eagerly
        register a dormant watcher's config on a connector that has no
        intention of starting it, purely because a DIFFERENT connector
        asked "is this name taken?". A later genuine message for that
        watcher's own room would then find the eagerly-registered-but-
        never-started config via `existing_by_name`/`existing_for_room`
        and treat it as an explicitly stopped watcher needing manual
        `resume` — even though its persisted state was never paused. This
        method answers the same "is it taken" question without ever
        mutating state, at the cost of not re-validating that the room
        still resolves or still matches the connector's current rule (an
        already-orphaned dynamic watcher is treated as still "known" —
        conservative, but never incorrectly permissive).
        """
        if self.get_watcher_config(name) is not None:
            return True
        state = self._state_store.load().get(name)
        return state is not None and state.dynamically_created

    def _build_watcher_config_from_rule(
        self, watcher_name: str, rule: WatcherConfig, room_name: str
    ) -> WatcherConfig:
        """Shared by try_lazy_create() and _reconstruct_dynamic_watcher_config()
        below — a concrete, single-room WatcherConfig derived from a
        room: "*" rule (docs/design/on-the-fly-watchers.md). Sticky
        session_id/exclude_rooms never carry over — neither concept applies
        once a real room is known."""
        return WatcherConfig(
            name=watcher_name,
            connector=rule.connector,
            room=room_name,
            agent=rule.agent,
            session_id=None,
            exclude_rooms=[],
            context_inject_files=rule.context_inject_files,
            online_notification=rule.online_notification,
            offline_notification=rule.offline_notification,
            history_handoff=rule.history_handoff,
        )

    async def _reconstruct_dynamic_watcher_config(self, name: str) -> "WatcherConfig | None":
        """Best-effort reconstruction of a lazily-created watcher's
        WatcherConfig after a restart (PR #79 review, third round).

        Its config is never persisted, only its WatcherState (via
        `dynamically_created`) — without this, pause_watcher()/
        resume_watcher()/reset_watcher() would reject a dynamically-created
        watcher as "not found in config" forever after a single restart,
        even via the one CLI path specifically meant to bring it back
        (e.g. `agent-chat-gateway resume <name>` immediately failing for
        exactly the watcher it's being asked to resume).

        Returns the reconstructed WatcherConfig — already appended to
        `self._watcher_configs`, with `self._states[name]` seeded from the
        persisted state too (so pause_watcher()/resume_watcher()/
        reset_watcher()'s own `self._states.get(name)` lookups find it —
        without this, resume_watcher() would still "succeed" but create a
        brand-new session instead of resuming the old one, since
        _provision_session() only ever consults `self._states`, never
        `self._state_store.load()` directly) — if the room can still be
        resolved and still matches this connector's active rule. Returns
        `None` if the persisted entry isn't a dynamically-created one, or
        the room/rule can no longer be resolved/matched (e.g. the wildcard
        rule was since removed, or the room is now in `exclude_room:`) —
        genuinely orphaned, nothing to reconstruct.
        """
        state = self._state_store.load().get(name)
        if state is None or not state.dynamically_created:
            return None
        if not self._watcher_rules:
            return None
        try:
            room = await self._connector.resolve_room_by_id(state.room_id)
        except Exception as e:
            logger.warning(
                "Could not reconstruct dynamic watcher '%s': failed to "
                "resolve room id=%s: %s", name, state.room_id, e,
            )
            return None
        rule = self._watcher_rules[0]
        if room.name in rule.exclude_rooms:
            return None
        wc = self._build_watcher_config_from_rule(name, rule, room.name)
        self._watcher_configs.append(wc)
        self._states[name] = state
        return wc

    async def can_find_or_reconstruct_watcher(self, name: str) -> bool:
        """Non-raising probe: True if `name` is either already a known
        WatcherConfig, or can be reconstructed as a dynamically-created one
        (see `_reconstruct_dynamic_watcher_config()` below).

        PR #79 review (fourth round): exists specifically for
        `ControlServer._find_entry_for_watcher()` (gateway/control.py),
        which routes `pause`/`resume`/`reset` CLI commands to the right
        connector by checking each one's `get_watcher_config()` — a plain,
        non-reconstructing, synchronous lookup. Without an async,
        reconstruction-aware probe like this one, a persisted-but-paused
        dynamically-created watcher's config is never in
        `_watcher_configs` post-restart, `_find_entry_for_watcher()` never
        finds an owning connector, and the CLI command never reaches
        `resume_watcher()`'s own (correctly reconstruction-aware) handling
        at all — the fix in that method was unreachable through the one
        interface real operators actually use.

        A `True` result has the same side effect `_find_or_reconstruct_watcher_config()`
        does when it reconstructs (appends to `_watcher_configs`, seeds
        `self._states`) — deliberate, so the caller's SUBSEQUENT
        `resume_watcher()`/etc. call finds it via the fast, plain
        `get_watcher_config()` path instead of reconstructing a second time.
        """
        if self.get_watcher_config(name) is not None:
            return True
        return await self._reconstruct_dynamic_watcher_config(name) is not None

    async def _find_or_reconstruct_watcher_config(self, name: str) -> WatcherConfig:
        """Shared by pause_watcher()/resume_watcher()/reset_watcher(): the
        plain `get_watcher_config()` lookup, with a fallback attempt to
        reconstruct a dynamically-created watcher's config (see
        `_reconstruct_dynamic_watcher_config()` above) before giving up.
        Raises RuntimeError (same message shape the old, now-removed
        `_find_watcher_config()` always raised) if neither finds anything."""
        wc = self.get_watcher_config(name)
        if wc is not None:
            return wc
        wc = await self._reconstruct_dynamic_watcher_config(name)
        if wc is not None:
            return wc
        raise RuntimeError(
            f"Watcher '{name}' not found in config. "
            f"Available: {[w.name for w in self._watcher_configs]}"
        )

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def stop_all(self) -> None:
        """Stop all processors concurrently (called during shutdown).

        Running stops in parallel is safe because:
          - Each processor's drain (the slow part, up to 30 s) is independent.
          - Shared connector state (room refcount, DDP unsubscribe) is updated
            before the first ``await`` inside each call, so asyncio cooperative
            scheduling guarantees no interleaving during those dict mutations.

        Without parallelism a two-watcher setup could take 60 s to drain before
        the backends are even stopped — exceeding the ``stop_daemon()`` grace
        window and leaving ``opencode serve`` as an orphan process.
        """
        names = list(self._processors)
        results = await asyncio.gather(
            *[self._stop_processor(name, save=False) for name in names],
            return_exceptions=True,
        )
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.error("Error stopping watcher '%s' during shutdown: %s", name, result)

    def save_state(self) -> None:
        """Persist current state (called before shutdown)."""
        self._state_store.save(self._states)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _start_watcher(
        self,
        wc: WatcherConfig,
    ) -> None:
        """Start a single watcher: resolve room, ensure session, start processor.

        Phases:
          1. Resolve agent and room.
          2. Provision session (reuse or create).
          3. Build state and register session maps.
          4. Build + ensure durable context delivery (identity header,
             addressing rules, context files); best-effort one-time history
             handoff send.
          5. Prepare attachment workspace.
          6. Create MessageProcessor (not yet started).
          7. Subscribe to connector (with rollback on failure).
          8. Register processor with dispatcher (deferred until subscribe succeeds).
          9. Activate processor (start consumer loop + online notification).
         10. Restore dedup watermark.

        Reads any prior state itself from `self._states.get(wc.name)` rather
        than taking it as a parameter (PR #79 review, fifteenth round).
        Findings #35 and #37 were both the same shape: a caller read a
        `WatcherState` snapshot, then acted on it after an `await` or a lock
        acquisition during which the real state could have changed (an
        agent-mismatch discard losing `dynamically_created`; a concurrent
        pause completing while a wake was still holding a stale unpaused
        snapshot). Self-reading here closes that whole class at once: every
        caller MUST hold `self._get_watcher_lock(wc.name)` for the duration
        of this call (asserted below) and, if its own prior state came from
        somewhere other than `self._states` (e.g. a disk read), must seed
        `self._states[wc.name]` with it BEFORE calling — there is no
        parameter left to smuggle a stale copy through.
        """
        assert self._get_watcher_lock(wc.name).locked(), (
            f"_start_watcher('{wc.name}') called without holding its "
            "per-watcher lock — every caller must acquire "
            "self._get_watcher_lock(wc.name) first, so this method's own "
            "self._states read below is guaranteed fresh."
        )
        state = self._states.get(wc.name)
        agent_name = self._resolve_agent_name(wc.agent)
        agent = self._agents[agent_name]
        agent_cfg = self._config.agent_config(agent_name)

        # 1. Resolve room
        room = await self._connector.resolve_room(wc.room)

        # PR #79 review (sixth round): a retained/persisted state under
        # this watcher's NAME might belong to a DIFFERENT room than the
        # one just resolved — e.g. a name that used to be a lazily-created
        # watcher's auto-generated name for some other room, later
        # reassigned to a static config entry for a different room (a
        # config edit, the wildcard rule being removed, or a sanitized-
        # name collision). sync_watchers()'s startup loop looks up
        # `persisted.get(wc.name)` by name alone with no room check at
        # all — reusing such a state's session_id/context_injected/
        # last_processed_ts here would leak that OTHER room's
        # conversation context into this one. Treat a room_id mismatch as
        # "no usable prior state" (same as a never-before-seen watcher)
        # rather than refusing to start — a configured watcher must still
        # be able to start; the safe response is a fresh session, not a
        # hard failure. (try_lazy_create()'s own equivalent check, added
        # in an earlier review round, refuses creation outright instead —
        # appropriate there since nothing has been configured yet to
        # start regardless; here `wc` is an explicit, real config entry
        # that must end up running one way or another.)
        if state is not None and state.room_id and state.room_id != room.id:
            logger.warning(
                "Watcher '%s': retained state belongs to a different room "
                "(room_id=%s) than the one just resolved (room_id=%s) — "
                "discarding it and starting a fresh session instead.",
                wc.name, state.room_id, room.id,
            )
            state = None

        # PR #79 review: a persisted session's agent may not match this
        # watcher's CURRENT resolved agent — e.g. a wildcard rule's
        # `agent:` was edited in config.yaml between restarts. Session IDs
        # are backend-specific: handing an old Claude session ID to
        # OpenCode (or vice versa) would either fail to resume or, worse,
        # attach to an unrelated session that happens to share the ID
        # format. `state.agent == ""` (every state persisted before this
        # field existed) is treated as "unknown — assume compatible" so
        # this doesn't force-reset every already-running watcher's session
        # on the first restart after this ships. Does not affect a pinned
        # `wc.session_id` — that short-circuits inside _provision_session()
        # before `state` is even consulted, by design; pinning a session ID
        # is an explicit, static operator choice, not something that can
        # silently drift the way a wildcard rule's `agent:` field can.
        if state is not None and state.agent and state.agent != agent_name:
            logger.warning(
                "Watcher '%s': retained state's session was created under "
                "agent '%s', but this watcher's active agent is now '%s' "
                "— discarding it and starting a fresh session instead "
                "(session IDs are backend-specific and cannot be safely "
                "reused across agents).",
                wc.name, state.agent, agent_name,
            )
            # Unlike the room-id mismatch just above (genuinely a
            # DIFFERENT room/watcher's leftover data — full discard is
            # correct there), this state IS still this exact watcher's own
            # identity: same room, same name, just needing a fresh session
            # under the new agent. A full `state = None` here would lose
            # `dynamically_created` along with the session fields — the
            # rest of this method sources it via `state.dynamically_created
            # if state else False`, so nulling `state` entirely silently
            # writes `dynamically_created=False` into the replacement
            # WatcherState below. Whether that's caught depends entirely on
            # the caller: try_lazy_create() re-sets the flag explicitly
            # right after calling this method, but resume_watcher()/
            # reset_watcher()/wake_dormant_watcher() do NOT — they rely
            # exactly on this carry-forward (the fifth round's own fix, see
            # its comment below at the `ws.dynamically_created=` line) to
            # keep it set. Losing it there means sync_watchers() prunes
            # this watcher as a removed one on the NEXT restart, abandoning
            # it a second time. Preserving dynamically_created/room_id/
            # room_type (all unaffected by an agent change) while
            # resetting only the session-specific fields keeps that
            # carry-forward working regardless of which caller reached here.
            state = WatcherState(
                watcher_name=state.watcher_name,
                session_id="",
                room_id=state.room_id,
                room_type=state.room_type,
                context_injected=False,
                paused=False,
                last_processed_ts="",
                dynamically_created=state.dynamically_created,
                agent="",
            )

        # 2. Provision session
        session_id, created_new_session = await self._provision_session(
            wc, state, agent, agent_cfg
        )

        # 3. Build state and register maps
        ws = WatcherState(
            watcher_name=wc.name,
            session_id=wc.session_id or session_id,
            room_id=room.id,
            room_type=room.type,
            context_injected=state.context_injected if state else False,
            paused=False,
            last_processed_ts=state.last_processed_ts if state else "",
            # PR #79 review (fifth round): _start_watcher() always builds a
            # BRAND NEW WatcherState here — without copying this flag
            # forward from the incoming `state`, a dynamically-created
            # watcher that's later legitimately resumed or reset would
            # have it silently reset to False (the dataclass default), so
            # sync_watchers() would then drop its state on the NEXT
            # restart after all, defeating that fix for any dynamic
            # watcher that's ever been resumed/reset even once.
            dynamically_created=state.dynamically_created if state else False,
            # Record which agent provisioned this session, regardless of
            # whether `state` was retained or discarded above — this is
            # what lets a FUTURE restart detect an agent change and
            # discard the stale session in turn.
            agent=agent_name,
        )
        self._states[wc.name] = ws
        self._maps.bind_session(session_id, room.id, self._connector)

        # 3.5 Fetch channel history for context handoff (new sessions only).
        # Only fires when created_new_session=True (reset, upgrade, first join)
        # so that resumed sessions (same session ID) are not re-injected.
        # Failure is non-fatal: a failed fetch logs a warning and the watcher
        # starts without history rather than blocking the entire startup.
        history_context: str | None = None
        hh = wc.history_handoff
        if created_new_session and hh.enabled:
            try:
                raw_msgs = await self._connector.fetch_room_history(room, hh.fetch_count)
                fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
                history_context = format_history_context(
                    raw_msgs, verbatim_tail=hh.verbatim_tail, fetched_at=fetched_at
                )
                if history_context:
                    logger.info(
                        "Watcher '%s': injecting history handoff (%d messages, %d chars)",
                        wc.name,
                        len(raw_msgs),
                        len(history_context),
                    )
            except Exception as e:
                logger.warning(
                    "Watcher '%s': history handoff fetch failed — starting without history: %s",
                    wc.name,
                    e,
                )

        # 3.6 Deliver history handoff as a simple, separate, best-effort one-time
        # send. Deliberate design decision (not an oversight): history_context is
        # genuinely one-time/volatile content — it gives the agent conversation
        # continuity across resets/upgrades, but it is not protocol-critical like
        # the identity/addressing header. It therefore does NOT get the shared
        # retry tracking that InjectedContextBuilder.ensure() provides below; a
        # failed send here just logs a warning and the watcher still starts.
        if history_context:
            try:
                await agent.send(
                    session_id=session_id,
                    prompt=history_context,
                    working_directory=agent_cfg.working_directory,
                    timeout=agent_cfg.timeout,
                )
            except Exception as e:
                logger.warning(
                    "Watcher '%s': history handoff send failed — continuing without it: %s",
                    wc.name,
                    e,
                )

        # 4. Build durable context (identity header + context files) and ensure
        # it durably reaches the agent (rollback maps on hard failure). This runs
        # UNCONDITIONALLY on every watcher start — including resumed sessions —
        # because backends like Claude have no side effect to skip; they must
        # return a fresh --append-system-prompt-file path every time.
        try:
            built_content = await self._injector.build(
                agent_name, wc.connector, wc,
                agent_username=self._connector.agent_username,
            )
            to_repeat = await self._injector.ensure(
                ws, session_id, agent, agent_cfg.working_directory, agent_cfg.timeout,
                watcher_name=wc.name, content=built_content,
            )
        except Exception:
            self._states.pop(wc.name, None)
            self._maps.remove_session(session_id)
            await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            raise

        # 5. Prepare attachment workspace
        # setup() contains multiple synchronous blocking filesystem calls
        # (mkdir, is_symlink, resolve, unlink, symlink_to, exists).  Running
        # them on the event loop would block all other coroutines during disk
        # I/O; asyncio.to_thread() offloads the whole operation to a thread
        # pool worker to keep the loop responsive.
        try:
            attachment_local_base = await asyncio.to_thread(
                self._attachment_workspace.setup,
                wc.name,
                room.id,
                agent_cfg.working_directory,
            )
        except Exception:
            # setup() failed (e.g., filesystem error, permission denied).  Roll
            # back the state and maps entries added in steps 3–4 so that a later
            # resume/restart sees a clean slate rather than a partially-built
            # WatcherState (with context_injected=True) and a dangling session
            # binding that would cause context re-injection to be skipped on the
            # next attempt.
            self._states.pop(wc.name, None)
            self._maps.remove_session(session_id)
            await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            raise

        # 6. Create processor (not started yet — activation deferred to step 9
        # so that the online notification is not emitted before subscribe succeeds).
        processor = MessageProcessor(
            session_id=session_id,
            room=room,
            working_directory=agent_cfg.working_directory,
            watcher_id=wc.name,
            connector=self._connector,
            agent=agent,
            agent_name=agent_name,
            config=self._config,
            permission_registry=self._permission_registry,
            session_role_map=self._maps.role,
            session_permission_thread_map=self._maps.permission_thread,
            session_maps=self._maps,
            context_injector=self._injector,
            watcher_state=ws,
            watcher_config=wc,
            connector_name=wc.connector,
            online_notification=wc.online_notification,
            offline_notification=wc.offline_notification,
            attachment_local_base=attachment_local_base,
            append_system_prompt_file=to_repeat,
        )
        self._processors[wc.name] = processor

        # 7. Subscribe (rollback everything on failure)
        try:
            await self._connector.subscribe_room(
                room,
                watcher_id=wc.name,
                working_directory=agent_cfg.working_directory,
            )
        except Exception:
            self._processors.pop(wc.name, None)
            # Keep ws in _states (do NOT pop) so that the context_injected flag
            # and session_id are preserved for the next _start_watcher call.
            cleaned = await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            if cleaned and created_new_session and not wc.session_id:
                ws.session_id = ""
                # The session that received context injection was destroyed, so
                # the next _start_watcher will create a brand-new session that
                # has never seen the context.  Reset the flag so injection is
                # re-attempted for the new session — without this, the new
                # session inherits context_injected=True from the old ws and
                # the agent silently operates without its system context.
                ws.context_injected = False
            self._states[wc.name] = ws
            self._maps.remove_session(session_id)
            raise

        # 8. Register with dispatcher — only after subscribe succeeds.
        self._dispatcher.add_processor(room.id, processor)

        # 9. Activate processor — starts the consumer loop and emits the
        # online notification.  Deferred to here so users never see "online"
        # for a watcher whose room subscription failed.
        processor.start()

        # 10. Restore watermark.
        # Only advance the room-level watermark; never move it backwards.
        # This matters when multiple watchers share the same room: a watcher that
        # was paused or reset may have an older persisted timestamp than a sibling
        # watcher that has been running and advancing the shared room watermark.
        # Writing an older value back would cause duplicate message delivery for
        # all watchers on that room after the next reconnect.
        if ws.last_processed_ts:
            current_ts = self._connector.get_last_processed_ts(room.id)
            if not current_ts or _ts_gt(ws.last_processed_ts, current_ts):
                self._connector.update_last_processed_ts(room.id, ws.last_processed_ts)

        logger.info(
            "Started watcher '%s' for room '%s' using agent '%s' (session %s)",
            wc.name,
            wc.room,
            agent_name,
            session_id[:8],
        )

    async def _provision_session(
        self,
        wc: WatcherConfig,
        state: WatcherState | None,
        agent: AgentBackend,
        agent_cfg,
    ) -> tuple[str, bool]:
        """Determine the session ID: reuse from config/state, or create a new one.

        Priority:
          1. Explicit ``wc.session_id`` from config (pinned session).
          2. Persisted ``state.session_id`` from a previous run.
          3. Create a new session via the agent backend.

        Agent/session compatibility is NOT re-checked here (PR #79 review)
        — the caller (`_start_watcher()`) already discards `state` entirely
        when `state.agent` doesn't match the watcher's current resolved
        agent, before this method ever sees it. A pinned `wc.session_id`
        (priority 1) is therefore never subject to that check at all — an
        explicit, static config choice is the operator's responsibility to
        keep consistent with `wc.agent`, unlike a wildcard rule's `agent:`,
        which can silently drift between restarts.
        """
        if wc.session_id:
            return wc.session_id, False
        if state and state.session_id:
            return state.session_id, False
        session_title = (
            f"{agent_cfg.session_prefix}:{wc.room}"
            if agent_cfg.session_prefix
            else None
        )
        session_id = await agent.create_session(
            agent_cfg.working_directory,
            extra_args=None,
            session_title=session_title,
        )
        logger.info("Watcher '%s': created new session %s", wc.name, session_id[:8])
        return session_id, True

    async def _cleanup_startup_session_best_effort(
        self,
        agent: AgentBackend,
        session_id: str,
        created_new_session: bool,
        watcher_name: str,
    ) -> bool:
        """Delete a freshly created session when watcher startup later fails."""
        if not created_new_session or not session_id:
            return False
        try:
            cleaned = await agent.delete_session(session_id)
            if cleaned:
                logger.info(
                    "Watcher '%s': cleaned up startup session %s after failure",
                    watcher_name,
                    session_id[:8],
                )
                return True
            logger.warning(
                "Watcher '%s': could not confirm cleanup of startup session %s",
                watcher_name,
                session_id[:8],
            )
            return False
        except Exception as e:
            logger.warning(
                "Watcher '%s': startup session cleanup failed for %s: %s",
                watcher_name,
                session_id[:8],
                e,
            )
            return False

    async def _stop_processor(self, name: str, save: bool) -> None:
        """Stop a processor and clean up all mappings.

        Order is critical for correctness:
          1. Remove from dispatcher — new inbound messages stop being routed here.
          2. Unsubscribe from connector — DDP stops delivering messages to this room.
          3. Stop the processor — drains any already-queued messages, then shuts down.
          4. Capture live watermark — after the queue is drained so the timestamp
             reflects the last message the processor *actually* handled.
          5. Clean session maps.
        """
        processor = self._processors.pop(name, None)
        state = self._states.get(name)
        wc = next((w for w in self._watcher_configs if w.name == name), None)
        errors: list[str] = []

        # Step 1: Remove from dispatcher so no new messages are routed to this processor.
        if processor and state and state.room_id:
            self._dispatcher.remove_processor(state.room_id, processor)

        # Step 2: Unsubscribe from the connector (stop DDP delivery for this room).
        if state and state.room_id:
            try:
                await self._connector.unsubscribe_room(state.room_id, watcher_id=name)
            except Exception as e:
                errors.append(f"unsubscribe failed: {e}")
                logger.error(
                    "Watcher '%s': unsubscribe failed for room '%s': %s",
                    name,
                    state.room_id,
                    e,
                )
        elif state:
            logger.debug(
                "Watcher '%s' has no room_id in state — skipping unsubscribe", name
            )

        # Step 3: Stop the processor (drain the queue; _stopping=True rejects late arrivals).
        if processor:
            try:
                await processor.stop()
            except Exception as e:
                errors.append(f"processor stop failed: {e}")
                logger.error("Watcher '%s': processor stop failed: %s", name, e)

        # Step 4: Capture the live watermark after the queue has been fully drained.
        if state and state.room_id:
            live_ts = self._connector.get_last_processed_ts(state.room_id)
            if live_ts:
                state.last_processed_ts = live_ts

        # Step 5: Clean up session maps.
        # Convention: empty string "" in session_id means "no session" (auto-create
        # mode, not yet assigned).  The falsy guard below skips cleanup in that case.
        if state:
            effective_session = (
                wc.session_id if wc and wc.session_id else state.session_id
            )
            if effective_session:
                if self._permission_registry:
                    self._permission_registry.cancel_session(effective_session)
                self._maps.remove_session(effective_session)

        if save:
            self._state_store.save(self._states)
        logger.info("Stopped processor for watcher '%s'", name)
        if errors:
            raise RuntimeError(
                f"Watcher '{name}' stop completed with errors: {'; '.join(errors)}"
            )

    def _ensure_agent_available(self, wc: WatcherConfig) -> None:
        """Fail closed if a watcher's resolved agent is currently unavailable."""
        agent_name = self._resolve_agent_name(wc.agent)
        if agent_name in self._blocked_agents:
            raise RuntimeError(
                f"Watcher '{wc.name}' cannot start because agent '{agent_name}' is unavailable"
            )

    def _resolve_agent_name(self, name: str | None) -> str:
        if name and name in self._agents:
            return name
        if name and name not in self._agents:
            logger.warning(
                "Agent '%s' not found in config, using default '%s'",
                name,
                self._default_agent,
            )
        return self._default_agent

    # Attachment symlink management has been extracted to
    # gateway.core.attachment_workspace.AttachmentWorkspace.
