"""WatcherLifecycle: manages watcher start/stop/pause/resume/reset.

Extracted from SessionManager to keep watcher management logic focused.
Owns the _processors and _states dicts, delegates to MessageDispatcher,
InjectedContextBuilder, and StateStore for their respective concerns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

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
        check_global_name_available: Callable[[str], bool] | None = None,
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
        # Cross-connector watcher-name uniqueness check (PR #79 review,
        # fourth round) — see GatewayService._is_watcher_name_globally_available()
        # for why this can't be answered from inside a single connector's
        # own WatcherLifecycle. Optional (defaults to "always available")
        # so every existing caller/test constructing a WatcherLifecycle
        # without a GatewayService above it keeps working unchanged — those
        # callers have no cross-connector name space to protect anyway.
        self._check_global_name_available = check_global_name_available or (lambda _name: True)

        self._processors: dict[str, MessageProcessor] = {}
        self._states: dict[str, WatcherState] = {}
        # Per-watcher mutex: prevents concurrent pause/resume/reset commands for
        # the same watcher from racing through _stop_processor / _start_watcher.
        # The control socket can serve multiple simultaneous clients, so two
        # commands targeting the same watcher could otherwise interleave and
        # corrupt _processors / _states.
        self._watcher_locks: dict[str, asyncio.Lock] = {}

    # ── Sync ──────────────────────────────────────────────────────────────────

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

        for wc in self._watcher_configs:
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
                    await self._start_watcher(wc, state)
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
                if persisted[name].dynamically_created:
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
                    self._states[name] = persisted[name]
                else:
                    logger.debug(
                        "Watcher '%s' not in current config — will be omitted from next state save",
                        name,
                    )

        self._state_store.save(self._states)
        return errors

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

            # Cross-connector check (PR #79 review, fourth round): the two
            # checks above only see THIS connector's own watchers. Watcher
            # names are assumed globally unique across every connector
            # (ControlServer._find_entry_for_watcher(), the scheduler) —
            # an invariant already enforced for static watchers at
            # config-load time, but never checked for a name generated at
            # runtime. Refusing here, rather than creating a same-name
            # watcher on two different connectors, keeps that assumption true.
            if not self._check_global_name_available(watcher_name):
                logger.error(
                    "try_lazy_create: auto-generated name '%s' for room "
                    "'%s' is already used by a watcher on a different "
                    "connector — refusing to create (watcher names must "
                    "be globally unique).",
                    watcher_name, room.name,
                )
                return False

            wc = self._build_watcher_config_from_rule(watcher_name, rule, room.name)
            # Resume a dormant session from a prior run, if one was
            # persisted under this exact (deterministic) name — same
            # resume-first behavior _provision_session() already gives
            # every static watcher.
            state = self._state_store.load().get(watcher_name)

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
                await self._start_watcher(wc, state)
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
                await self._start_watcher(wc, state)
            except Exception as e:
                logger.error("Failed to resume watcher '%s': %s", name, e)
                raise
            # Only clear paused flag AFTER successful start — if _start_watcher() raises,
            # the watcher is still stopped and the paused flag should remain True in memory
            # so the next restart (or manual retry) correctly reflects the watcher's state.
            if state:
                state.paused = False
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
                if not wc.session_id:
                    state.session_id = ""
                state.context_injected = False
                state.paused = False

            try:
                await self._start_watcher(wc, state)
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

    def get_watcher_config(self, watcher_name: str) -> "WatcherConfig | None":
        """Return the WatcherConfig for a watcher name, or None if not found."""
        return next((wc for wc in self._watcher_configs if wc.name == watcher_name), None)

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
        state: WatcherState | None,
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
        """
        agent_name = self._resolve_agent_name(wc.agent)
        agent = self._agents[agent_name]
        agent_cfg = self._config.agent_config(agent_name)

        # 1. Resolve room
        room = await self._connector.resolve_room(wc.room)

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
