"""WatcherLifecycle: manages watcher start/stop/pause/resume/reset.

Extracted from SessionManager to keep watcher management logic focused.
Owns the _processors and _states dicts, delegates to MessageDispatcher,
InjectedContextBuilder, and StateStore for their respective concerns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..agents import AgentBackend
from .adapter_utils import ts_gt as _ts_gt
from .attachment_workspace import AttachmentWorkspace
from .config import CoreConfig, WatcherConfig
from .connector import Connector, Room
from .dispatch import MessageDispatcher, RoomAlreadyRoutedError
from .history_context import format_history_context
from .injected_context_builder import InjectedContextBuilder
from .message_processor import MessageProcessor
from .paths import room_path_key, watcher_prompt_key
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import (
    StateFilter,
    WatcherState,
    backend_identity,
    carried_fields,
    lifecycle_state,
    now_iso,
    past_expire_ttl,
    past_idle_ttl,
    state_filter_name,
)
from .state_store import StateStore

# No cycle: watcher_manager's runtime imports reach state/config/connector,
# never back into this module (its lifecycle reference is TYPE_CHECKING only).
from .watcher_manager import config_from_record

logger = logging.getLogger("agent-chat-gateway.core.watcher_lifecycle")


def _should_restore_watermark(stored: str, live: str | None) -> bool:
    """Whether a watcher's persisted watermark may be written into the connector.

    The connector gives three answers, and the middle one is why this is a function
    rather than a condition:

    * ``None`` — no state for this room. The record is all there is; restore it.
    * ``""``   — cleared on purpose, because the account is no longer in the room.
      Restoring would undo that, and a later re-add would replay the interval it was
      absent for. This is the case a `not current_ts` test got wrong in one direction
      and a bare `is None` test got wrong in the other: the comparison below still
      ran, and `ts_gt(anything, "")` is True.
    * a value  — the room's live cursor. Restore only when the record is ahead of it,
      never backwards: the connector advances that cursor as messages are accepted,
      independently of any one watcher's restarts, and writing an older value back
      would redeliver everything between the two at the next reconnect.
    """
    if not stored:
        return False
    if live is None:
        return True
    if live == "":
        return False
    return _ts_gt(stored, live)


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
        state_store: StateStore,
        dispatcher: MessageDispatcher,
        injector: InjectedContextBuilder,
        permission_registry: PermissionRegistry | None,
        maps: SessionMaps,
    ) -> None:
        self._connector = connector
        self._agents = agents
        self._default_agent = default_agent
        self._config = config
        self._state_store = state_store
        self._dispatcher = dispatcher
        self._injector = injector
        self._permission_registry = permission_registry
        self._maps = maps
        self._attachment_workspace = AttachmentWorkspace(connector)
        self._blocked_agents: set[str] = set()

        self._processors: dict[str, MessageProcessor] = {}
        self._states: dict[str, WatcherState] = {}
        # Per-watcher mutex: prevents concurrent pause/resume/reset commands for
        # the same watcher from racing through _stop_processor / the start.
        # The control socket can serve multiple simultaneous clients, so two
        # commands targeting the same watcher could otherwise interleave and
        # corrupt _processors / _states.
        self._watcher_locks: dict[str, asyncio.Lock] = {}

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync_watchers(
        self, unavailable_agents: set[str] | None = None
    ) -> list[str]:
        """Hydrate the persisted records and settle agent availability at boot.

        The static start loop that used to live here died at cutover: nothing
        produces a static `WatcherConfig` any more, so every watcher's
        recreation source is its persisted record (rule-derived, §2.4) or the
        eager-start loop for connectors with no inbound stream (§2.6). What
        remains is what boot still owes:

        * **Agent availability.** Recorded only when the caller explicitly
          provides it — None means "no information", not "all available",
          or a second call would silently disarm the fail-closed
          `_ensure_agent_available` guard in resume/reset.
        * **Hydration.** Rule-derived records are loaded into memory so
          `record_for_room` answers from boot — idle, until a message, the
          replay or the eager loop recreates them.
        * **The prune.** A record with no `rule_name` is the static model's,
          and the static model has no owner left: config.yaml cannot name it
          and no rule will recreate it. Pruned, with a log line each —
          per the clean-break migration ruling, and warned about ahead of
          time by `acg config validate`'s orphan check.

        Returns the startup error list (now fed only by the eager-start loop,
        which appends to it in `SessionManager.sync_only`).
        """
        errors: list[str] = []
        persisted = self._state_store.load()
        if unavailable_agents is not None:
            self._blocked_agents = set(unavailable_agents)

        prune = {name for name, ws in persisted.items() if not ws.rule_name}
        for name in sorted(prune):
            logger.warning(
                "Pruning static-era watcher record '%s' — the static shape "
                "was removed and nothing recreates it; add a rule matching "
                "its room to serve the room again (a fresh session starts on "
                "its first message)", name,
            )
        for name, ws in persisted.items():
            if ws.rule_name and name not in self._states:
                self._states[name] = ws

        self._state_store.save(self._states, prune=prune)
        return errors

    def _get_watcher_lock(self, name: str) -> asyncio.Lock:
        """Return (creating if needed) the per-watcher mutex for lifecycle ops."""
        if name not in self._watcher_locks:
            self._watcher_locks[name] = asyncio.Lock()
        return self._watcher_locks[name]

    def watcher_lock(self, name: str) -> asyncio.Lock:
        """The per-watcher mutex, for callers *outside* the lifecycle (§2.5).

        The manager's create/recreate takes this around the start it drives, so
        a wake cannot interleave with a pause's or an idle drop's teardown of
        the same watcher: without it, a message landing mid-drain recreates the
        watcher against the state object the teardown is still dismantling, and
        the teardown's last step then removes the session binding the
        recreation just made. Lock ordering is the manager's per-room lock
        outer, this lock inner; nothing takes them reversed.

        **Never taken inside `start_watcher_in_room`** — `sync_watchers` already
        holds it when it reaches that method, and the lock is not reentrant.
        """
        return self._get_watcher_lock(name)

    # ── Lifecycle controls ────────────────────────────────────────────────────

    async def pause_watcher(self, name: str) -> None:
        """Pause a watcher: stop processing messages but preserve state.

        Pause acts on a **record** (§4.4): a name with no record is rejected —
        an unobserved room has no id, no kind, nothing to key on — and the
        rejection points at the rule's `except_for:`, which is where "never
        engage with this room" belongs: declarative and effective before the
        first message. The old path fabricated a blank record for such a
        name, which is #118's defect 1: the blank became the surviving copy
        on save, and the real session id was unrecoverable.
        """
        record = self._states.get(name)
        if record is None or not record.config:
            raise RuntimeError(
                f"No watcher named '{name}' — pause acts on a record, and "
                f"a room the gateway has never seen has none (§4.4). To "
                f"keep the bot out of a room durably, add the room to the "
                f"rule's 'rooms.except_for' list instead."
            )
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if state and state.paused:
                logger.info("Watcher '%s' is already paused", name)
                return
            try:
                await self._stop_processor(name)
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
            self._state_store.save(self._states)
            logger.info("Watcher '%s' paused", name)

    async def drop_idle(self, name: str, *, now) -> bool:
        """The idle drop (§2.5): release the runtime, keep everything a wake needs.

        Deliberately **narrower than `_stop_processor`**, and not a flag on it:
        that method unsubscribes, and the idle savings exist precisely because
        an idle room stays subscribed — the connector's room entry, watermark
        and seen-id window are what make recreation cheap and dedup seamless
        (§2.2). Idle teardown and pause teardown are different operations that
        happen to share steps.

        What it does, in `_stop_processor`'s numbering: 1 (release the
        dispatcher slot), 2 (capture the live watermark into the record), 4
        (drain and stop the processor), 5 (clean the session maps — the wake's
        recreation re-binds, exactly as the restart-shaped recreation already
        does). Never 3. Then `dropped_at` is stamped from the sweep's own
        clock, so one pass reads one instant, and the record is saved.

        Every decision is re-checked under the per-watcher lock: between the
        sweep's look and this acquisition, an enqueue can advance the activity
        clock, an operator can pause, a turn can start. Answers False — and
        changes nothing — unless the drop actually happened. `now` is the
        sweep's injected clock (aware datetime).
        """
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if state is None or state.paused or state.dropped_at:
                return False
            processor = self._processors.get(name)
            if processor is None:
                # Not resident: failed, or mid-transition. Boot owns failed
                # records (§2.5, retry-at-every-start); a timer must not.
                return False
            if processor.has_work_in_flight:
                return False
            if (
                self._permission_registry is not None
                and state.session_id
                and self._permission_registry.pending_for_session(state.session_id)
            ):
                # An approval an operator is still reading — an idle drop must
                # not cancel it (§2.5).
                return False
            if not past_idle_ttl(state, now):
                return False

            self._processors.pop(name, None)
            if state.room_id:
                self._dispatcher.remove_processor(state.room_id, processor)
                # Capture the live watermark while the connector still holds the
                # room entry — same reason as `_stop_processor` step 2, same
                # `is not None` rule: None means "no opinion", an empty string
                # is one.
                live_ts = self._connector.get_last_processed_ts(state.room_id)
                if live_ts is not None:
                    state.last_processed_ts = live_ts
            try:
                await processor.stop()
            except Exception as e:
                logger.warning(
                    "Watcher '%s': processor stop failed during idle drop "
                    "(proceeding — the slot and record are already settled): %s",
                    name, e,
                )
            if state.session_id:
                self._maps.remove_session(state.session_id)
            state.dropped_at = now.isoformat(timespec="seconds")
            self._state_store.save(self._states)
            logger.info(
                "Watcher '%s' idled after %s day(s) without activity — session "
                "kept, room still subscribed; its next message wakes it",
                name, (state.rule or {}).get("session_idle_days"),
            )
            return True

    async def expire_idle(self, name: str, *, now, pending_jobs=None) -> bool:
        """The expiry (§2.5): reclaim everything an idle record points at.

        The destructive leg — after this the room has no record, no watermark
        and no session, and its next message creates a *fresh* watcher through
        `_create` against the current rules. Reclaimed, in order: the
        connector's room state, the backend session, injector retry state,
        session maps, the system-prompt file, the attachment symlink and
        cache, and finally the record itself. "Expiry reclaims everything, or
        it leaves bookkeeping behind"; each best-effort step logs once and
        accepts its leak rather than refusing to expire. The reclamation
        itself — and the unsubscribe-first / record-popped-last ordering that
        makes it safe — lives in `_reclaim_record_locked`, shared with
        membership removal; only the gates are expiry's own.

        Every decision re-checks under the per-watcher lock, like `drop_idle`:
        between the sweep's look and this acquisition a wake can make the
        record resident again, and a resident record is not idle.
        """
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if state is None or state.paused or not state.dropped_at:
                return False
            if self._processors.get(name) is not None:
                return False
            if not past_expire_ttl(state, now):
                return False
            if pending_jobs is not None and pending_jobs(name):
                # Re-checked UNDER the lock (Codex review of #121): the
                # sweep's own check ran before this coroutine was scheduled,
                # and a schedule-create landing in that gap would have its
                # job orphaned by the reclamation below. The residual window
                # — a create between this line and the pop — is accepted:
                # closing it would couple the job store's writes to the
                # watcher lock, and the job store lives a layer up.
                logger.info(
                    "Watcher '%s' gained a pending job while its expiry "
                    "waited — not expiring", name,
                )
                return False

            await self._reclaim_record_locked(name, state)
            logger.info(
                "Watcher '%s' expired after %s day(s) idle — session and "
                "record reclaimed; the room's next message creates a fresh "
                "watcher", name, (state.rule or {}).get("session_expire_days"),
            )
            return True

    async def _reclaim_record_locked(self, name: str, state: WatcherState) -> None:
        """Reclaim everything a record points at, record popped last (§2.5).

        The shared destructive body: expiry calls it after its gates
        (TTL, residency, pause, the job exemption), membership removal after
        its own (stop a resident processor, cancel jobs, audit the pause
        override). The gates stay with the callers on purpose — they are what
        differ; the reclamation order is what must not.

        Caller MUST hold the per-watcher lock, and logs its own outcome —
        this body logs only its per-step leaks.

        **The unsubscribe runs first, and the record is popped last** — both
        halves are load-bearing. Unsubscribing first funnels every mid-reclaim
        frame onto the *untracked* path, whose episode reads the record under
        the caller's lock and hits `_recreate`'s staleness re-check once we
        finish; popping first instead would hand a mid-await frame to
        `_create` while this teardown still owns the room's connector state.
        Popping last is crash-honesty: a crash anywhere before it leaves a
        record that is simply reclaimed again next time, while everything
        already reclaimed was best-effort to begin with.
        """
        # 1. The connector's room state. From here the room's frames take
        # the untracked path; under subscribe-all this is local bookkeeping
        # only, and calling it again after a partial failure is a no-op.
        if state.room_id:
            try:
                await self._connector.unsubscribe_room(
                    state.room_id, watcher_id=name)
            except Exception as e:
                logger.warning(
                    "Watcher '%s': unsubscribe failed during reclamation "
                    "(proceeding): %s", name, e,
                )

        agent_name = self._resolve_agent_name(state.agent or None)
        agent = self._agents.get(agent_name)
        session_id = state.session_id

        # 2. The backend session. False means unsupported or unconfirmed —
        # logged and accepted, per §2.5.
        if session_id and agent is not None:
            try:
                if not await agent.delete_session(session_id):
                    logger.info(
                        "Watcher '%s': backend did not confirm deletion of "
                        "session %s — accepting the leak", name, session_id,
                    )
            except Exception as e:
                logger.warning(
                    "Watcher '%s': backend session delete failed — "
                    "accepting the leak: %s", name, e,
                )

        # 3. Injector retry state and session maps. Both idempotent; the
        # maps entry was already removed by the idle drop.
        if session_id:
            self._injector.reset_session(session_id)
            self._maps.remove_session(session_id)

        # 4. The system-prompt file — the backend that wrote it removes it.
        if agent is not None and state.room_id and state.connector:
            try:
                await agent.reclaim_durable_instructions(
                    watcher_prompt_key(state.connector, state.room_id, name))
            except Exception as e:
                logger.warning(
                    "Watcher '%s': could not reclaim the prompt file: %s",
                    name, e,
                )

        # 5. The attachment symlink and cache directory.
        if state.room_id and state.connector:
            agent_cfg = self._config.agent_config(agent_name)
            try:
                await asyncio.to_thread(
                    self._attachment_workspace.reclaim,
                    room_path_key(state.connector, state.room_id),
                    state.room_id,
                    agent_cfg.working_directory,
                )
            except Exception as e:
                logger.warning(
                    "Watcher '%s': could not reclaim the attachment "
                    "workspace: %s", name, e,
                )

        # 6. The record, last.
        self._states.pop(name, None)
        self._state_store.save(self._states)

    async def reclaim_room(self, room_id: str, *, reason: str) -> str | None:
        """Forced reclamation of a room's record — not a timer's (§2.7, §4.4).

        Three callers, one semantics: a live membership removal, the periodic
        reconciliation discovering one that was missed, and the operator's
        `expire` verb. Each is an authoritative statement, never an inference
        from inactivity — which is why the gates below differ from expiry's.

        Reclaims the room's record through `_reclaim_record_locked` — the same
        body expiry runs — under gates that are almost all *weaker* than
        expiry's, because a removal is not an inference from inactivity:

        * **A resident processor is stopped first.** Expiry bails on residency;
          a remove can hit a live, active watcher (the bot kicked from a room
          mid-conversation), and leaving its processor running against a popped
          record is the defect, not the teardown. The full `_stop_processor`
          is correct here — the room is gone, so there is no §2.2 connector
          state worth preserving the way an idle drop preserves it.
        * **Pause is overridden, audibly.** Pause protects a record from
          inactivity-driven reclamation; removal is the platform stating the
          room cannot receive another message, and honouring pause would keep
          a session, prompt file and attachment directory alive forever for
          it — and leave `resume` able to "revive" a room the bot has no
          access to. The override is the one case an operator's explicit
          setting is discarded, so it is logged as an audit event, never
          silent.
        * **No TTL, no job exemption.** The caller cancels this room's pending
          jobs with a stated reason — an exemption would keep a record alive
          for a job that can never deliver.

        One gate is *stronger*: a record with no materialized config is the
        static path's (config.yaml recreates it at every boot regardless of
        records), and reclaiming it here would delete a watermark while
        leaving the watcher's owner intent in place — skipped with a log,
        matching `_recreate`'s ownership rule.

        Idempotent: no record for the room answers None, so a removal
        discovered twice — the live event and a later REST failure, or the
        periodic reconciliation — reaches the same end state. Returns the
        reclaimed watcher's name so the caller can cancel its jobs.
        """
        while True:
            record = self.record_for_room(room_id)
            if record is None:
                return None
            name = record.watcher_name
            async with self._get_watcher_lock(name):
                if self._states.get(name) is not record:
                    # The record changed while we waited — an expiry reclaimed
                    # it, or a wake replaced it. Re-read and re-decide; the
                    # removal applies to the room, not to one snapshot of it.
                    continue
                if not record.config:
                    logger.info(
                        "Room %s's record ('%s') carries no materialized "
                        "config — its owner is config.yaml, so a membership "
                        "removal does not reclaim it (%s)",
                        room_id, name, reason,
                    )
                    return None
                if self._processors.get(name) is not None:
                    try:
                        await self._stop_processor(name)
                    except Exception as e:
                        logger.warning(
                            "Watcher '%s': error stopping the processor for a "
                            "membership removal (proceeding to reclaim): %s",
                            name, e,
                        )
                if record.paused:
                    logger.warning(
                        "AUDIT: watcher '%s' (room %s) was paused, and its "
                        "pause is being overridden by a forced reclamation — "
                        "%s. The pause protected the record from "
                        "inactivity-driven timers; this reclamation is not "
                        "one (§4.4).",
                        name, room_id, reason,
                    )
                await self._reclaim_record_locked(name, record)
                logger.info(
                    "Watcher '%s' reclaimed — %s; re-adding the bot to room "
                    "%s starts fresh, with no continuity",
                    name, reason, room_id,
                )
                return name

    def register_idle_record(self, wc: WatcherConfig, room: Room,
                             provenance: dict) -> None:
        """Membership add (§2.7): persist a record in idle state, start nothing.

        The sessionless sibling of `start_watcher_in_room`'s step 3: the same
        construction — provenance applied at construction, unknown keys
        refused — with every session-scoped field empty, because no session
        exists. No maps binding (nothing to bind), no dispatcher claim, no
        subscription: the room's first message takes the untracked path, and
        the routing episode finds this record and wakes it through
        `_recreate`, where `_provision_session` treats the empty session id
        as "none yet" and mints one.

        Caller (the manager's `register_on_join`) holds both the per-room and
        the per-watcher lock, and has already established no record exists
        for the room.
        """
        ws = WatcherState(
            watcher_name=wc.name,
            session_id="",
            room_id=room.id,
            room_type=room.type,
            room_name=room.name,
            context_injected=False,
            paused=False,
            last_processed_ts="",
            backend_identity="",
            **provenance,
        )
        self._states[wc.name] = ws
        self._state_store.save(self._states)

    async def resume_watcher(self, name: str) -> None:
        """Resume a paused watcher.

        A rule-derived record resumes from itself (§2.8): its own materialized
        config, a Room built from its own fields — a group DM has no name to
        resolve, so the static path's name resolution is wrong for it — and
        `carried_fields`, so the frozen snapshots survive the fresh
        `WatcherState` the start constructs. A resume that passed bare
        provenance would wipe `rule`/`config`/`created_at`, and the next boot
        would prune the emptied record as an orphan (the A1 lesson, again).
        """
        record = self._states.get(name)
        wc = config_from_record(record) if record is not None else None
        if wc is None:
            raise RuntimeError(
                f"No watcher named '{name}' — resume acts on a persisted "
                f"record, and this name has none. See 'list' for the "
                f"records that exist."
            )
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
            if state is None or not state.config:
                # The record was reclaimed while we waited on the lock (an
                # expire, or a membership removal). Same rule as everywhere:
                # re-read under the lock, and a record that is gone cannot
                # be resumed.
                raise RuntimeError(
                    f"Watcher '{name}' was reclaimed while the resume waited "
                    f"— its record is gone. The room's next message creates "
                    f"a fresh watcher."
                )
            try:
                await self._resume_record(wc, state)
            except Exception as e:
                logger.error("Failed to resume watcher '%s': %s", name, e)
                raise
            # The start constructed a fresh WatcherState with paused=False —
            # re-read and save what is actually in the map.
            self._state_store.save(self._states)
            logger.info("Watcher '%s' resumed", name)

    async def _resume_record(self, wc: WatcherConfig, record: WatcherState) -> None:
        """Start a rule-derived record's watcher again, for `resume_watcher`.

        Caller holds the per-watcher lock. Mirrors `_recreate`'s carry —
        `carried_fields`, `dropped_at` cleared — with two deliberate
        differences:

        * **`last_activity_at` is stamped at the moment of resume.** §2.5 rules
          this explicitly ("resume returns a paused watcher to active and
          restarts its clock"), and it is the one deliberate exception to
          *a recreation carries the clock, never restamps* (`_recreate`, the
          sixth inversion). The two write sites disagree on purpose: a boot or
          wake recreation is residency, not activity, but a watcher paused
          longer than its idle TTL must not be re-idled by the first sweep
          pass after an operator explicitly asked for it back.
        * **No replay.** Messages that arrived while paused were deliberately
          dropped, not deferred (§4.4) — a resume that replayed the paused
          interval would deliver exactly what the pause existed to mute.
        """
        carried = carried_fields(record)
        carried["dropped_at"] = ""
        carried["last_activity_at"] = now_iso()
        room = Room(
            id=record.room_id,
            name=record.room_name or record.watcher_name,
            type=record.room_kind or record.room_type,
        )
        await self.start_watcher_in_room(wc, record, room, provenance=carried)

    async def reset_watcher(self, name: str) -> None:
        """Reset a watcher: clear session and restart with fresh state.

        A **paused record is refused, loudly** — §2.5: "`reset` must not
        silently clear `paused`". A pause is the operator's only durable
        mute now that config no longer names rooms, so a reset that quietly
        un-muted would erase an explicit instruction as a side effect of
        session hygiene. The operator resumes first, then resets.
        """
        record = self._states.get(name)
        wc = config_from_record(record) if record is not None else None
        if wc is None:
            raise RuntimeError(
                f"No watcher named '{name}' — reset acts on a persisted "
                f"record, and this name has none. See 'list' for the "
                f"records that exist."
            )
        if record.paused:
            raise RuntimeError(
                f"Watcher '{name}' is paused — reset does not clear a pause "
                f"(§2.5). Resume it first, then reset."
            )
        self._ensure_agent_available(wc)
        async with self._get_watcher_lock(name):
            try:
                await self._stop_processor(name)
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
            if state is None or not state.config:
                # Reclaimed while the reset waited on the lock — same re-read
                # rule as resume's.
                raise RuntimeError(
                    f"Watcher '{name}' was reclaimed while the reset waited "
                    f"— its record is gone. The room's next message creates "
                    f"a fresh watcher."
                )
            # Clear injection retry state BEFORE resetting context_injected so
            # the new startup attempt begins with a fresh failure counter.
            # Without this, a watcher that reached ``failed_degraded`` would
            # immediately re-enter that state after reset (the old failure_count
            # is still at ``_MAX_INJECT_ATTEMPTS``, so one more failure tips it
            # over again).
            old_session_id = state.session_id
            if old_session_id:
                self._injector.reset_session(old_session_id)
            state.session_id = ""
            state.context_injected = False

            try:
                # The cleared session id means the start mints a fresh
                # session; the carry keeps the frozen snapshots, and the
                # reset stamps the clock for the same reason resume does — a
                # fresh session immediately re-idled by the next sweep pass
                # is the §2.5 misimplementation.
                await self._resume_record(wc, state)
            except Exception as e:
                logger.error("Failed to restart watcher '%s' after reset: %s", name, e)
                raise
            self._state_store.save(self._states)
            logger.info("Watcher '%s' reset", name)

    def list_watchers(
        self, state_filter: StateFilter = StateFilter.OPERABLE
    ) -> list[dict]:
        """Return info for persisted watcher records matching ``state_filter``.

        **Enumerates records, not config and not live processors** (design §2.8).
        Under rule-derived watchers there is no static set of watchers to read
        out of ``config.yaml``, and deriving the rows from ``self._processors``
        would make idle and paused rooms invisible to the very commands that can
        still act on them.

        Two consequences on the static path, both deliberate:

        * A configured watcher with no record — its agent was unavailable, or
          its first start raised before a record was written — does not appear.
          There is nothing in such a row but its name; ``sync_watchers`` reports
          the failure through its error list, which is where a start-time
          failure belongs.  A watcher that started on an *earlier* boot does
          still appear, because its record is on disk — as ``failed``, since it
          is not resident.
        * ``room_name`` and ``participants`` come from the record rather than
          from config, so they describe the room the watcher is actually bound
          to. (``room_kind`` is deliberately **not** in the row: nothing reads
          it.)
        """
        result = []
        for name, state in sorted(self._state_store.merged_view(self._states).items()):
            current = lifecycle_state(state, resident=self._is_resident(name))
            if current not in state_filter:
                continue
            result.append(
                {
                    "watcher_name": name,
                    # Falls back to the room id so a nameless room is still
                    # nameable in the table. Reachable: `pause` on a watcher
                    # that has never started fabricates a record with neither
                    # (both empty), and a rule-derived group DM has no platform
                    # name either. NOT the DM case — both connectors return the
                    # configured `@handle` as a DM room's name.
                    "room_name": state.room_name or state.room_id,
                    "room_id": state.room_id,
                    "participants": list(state.participants),
                    # The record's own connector/agent once the manager writes
                    # them; on the static path they are empty, so fall back to
                    # the entry this lifecycle belongs to and to config.
                    "connector": state.connector or self._state_store.state_name,
                    "agent_name": state.agent,
                    "session_id": state.session_id,
                    "state": state_filter_name(current),
                    "context_injection_state": (
                        self._injector.status_for(state.session_id).state
                        if state.session_id
                        else "not_started"
                    ),
                }
            )
        return result

    def _is_resident(self, name: str) -> bool:
        """Whether the lifecycle currently holds this watcher (design §2.5).

        A registered processor, **or** a lifecycle transition in flight. The
        second half is not a nicety: `pause` and `reset` both remove the
        processor first and settle the record last, so between the two this
        watcher has no processor, no `paused` flag and no `dropped_at` —
        indistinguishable from what a failed start leaves behind. `reset` stays
        that way for as long as a fresh session, a history fetch and a full
        model turn take, which is bounded by the agent timeout and defaults to
        minutes.

        Reporting `failed` there would be the worst kind of wrong: `failed` is
        documented as *the* state that means something is broken and sends the
        operator to the startup log, so the recovery verb would accuse itself
        while working. Counting a transition as resident errs the other way —
        `active` for a few seconds while a watcher is being stopped — which is
        transient, self-correcting, and does not send anyone anywhere.

        The per-watcher lock is exactly the right signal because it is held for
        precisely the span of a lifecycle transition: `sync_watchers`, `pause`,
        `resume` and `reset` all take it around their whole start or stop, and
        release it when the operation finishes — including when it *fails*, so a
        genuinely failed start reports `failed` the moment it gives up.
        """
        if self._processors.get(name) is not None:
            return True
        lock = self._watcher_locks.get(name)
        return lock is not None and lock.locked()

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
            *[self._stop_processor(name) for name in names],
            return_exceptions=True,
        )
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.error("Error stopping watcher '%s' during shutdown: %s", name, result)

    def save_state(self) -> None:
        """Persist current state (called before shutdown)."""
        self._state_store.save(self._states)

    # ── Read surface for the WatcherManager ──────────────────────────────────
    #
    # The manager owns the creation path (§2.7/§2.8) and this lifecycle owns the
    # start machinery; these three are the whole seam between them, so neither
    # reaches into the other's dicts.

    def record_for_room(self, room_id: str) -> WatcherState | None:
        """The in-memory record bound to a room, if any (§2.4 sticky binding).

        A linear scan because `_states` is keyed by watcher name until cutover
        re-keys it to `(connector, room_id)` — and it is only consulted for
        rooms with no live processor, which is the rare path.
        """
        for ws in self._states.values():
            if ws.room_id == room_id:
                return ws
        return None

    def processor_named(self, name: str) -> MessageProcessor | None:
        """The live processor for a watcher name, or None when not resident."""
        return self._processors.get(name)

    def states(self) -> dict[str, WatcherState]:
        """The in-memory records, by watcher name — the startup replay's
        iteration source. After `sync_watchers` this includes hydrated
        rule-derived records, which is the point: they are exactly the rooms a
        restart would otherwise forget."""
        return self._states

    def resolve_agent_name(self, ref: str) -> str:
        """Public form of `_resolve_agent_name`, for the record's `agent` field —
        the record must hold the *resolved* name, because recreation reads the
        record long after the default it was resolved against may have changed."""
        return self._resolve_agent_name(ref)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def start_watcher_in_room(
        self,
        wc: WatcherConfig,
        state: WatcherState | None,
        room: Room,
        history_before_ts: str | None = None,
        provenance: dict | None = None,
    ) -> None:
        """The one start path: session, state, context, workspace, processor.

        Every caller arrives holding a classified room — id, kind,
        description — so nothing here resolves by name (a group DM has no
        resolvable name at all). The callers are the manager's create and
        recreate, and the lifecycle's own `_resume_record`; the static
        `_start_watcher` that used to resolve a configured name into this
        method died at cutover.

        ``provenance`` carries the §5.3 fields a start does not rebuild — the
        frozen rule and config snapshots, the room kind, the lifecycle clocks
        (`gateway/core/state.py`, `carried_fields`). It is applied **at
        construction**, in step 3, rather than being written onto the record
        afterwards, and both halves of that matter:

        * a recreation that did not carry them wiped the snapshot recreation
          itself reads, and the next boot pruned the emptied record as an
          orphan — a room's session lost after two restarts, silently;
        * enriching after the start meant a concurrent creation's `save_state`
          could persist this record while it was still half-written, so a crash
          in that window left a rule-less record on disk to be pruned.
        """
        # 0. Fail closed on an unavailable agent, on EVERY start. The guard
        # used to live only in resume/reset, which left the creation paths —
        # message-triggered, eager, wake — able to start a watcher whose
        # permission broker never came up: processing messages with no
        # tool-call enforcement, silently. A raise here is an abort, not a
        # decision (§2.2): the message stays redeliverable, and the eager
        # loop reports it as a startup error.
        #
        # And fail closed on a frozen agent that no longer EXISTS (Codex
        # review of #121): `_resolve_agent_name` substitutes the default for
        # an unknown name, which is right for an empty field and wrong for a
        # named one — a record frozen against a since-deleted agent would
        # silently restart under a different backend, working directory and
        # tool policy, when sticky binding (§2.4) says the record is
        # authoritative. The watcher reads `failed` instead, which is honest:
        # the operator deleted the agent this room runs on.
        if wc.agent and wc.agent not in self._agents:
            raise RuntimeError(
                f"Watcher '{wc.name}' is bound to agent '{wc.agent}', which "
                f"no longer exists in config — refusing to substitute "
                f"'{self._default_agent}'. Restore the agent, or expire the "
                f"watcher to re-create it under the current rules."
            )
        self._ensure_agent_available(wc)
        agent_name = self._resolve_agent_name(wc.agent)
        agent = self._agents[agent_name]
        agent_cfg = self._config.agent_config(agent_name)

        # 1.5 Refuse a room another watcher already serves, before anything is built.
        # The authoritative claim is step 8, after the room is subscribed — reaching it
        # first would mean creating a session, injecting context and subscribing, then
        # undoing all of it. `holder` is a read, so this is check-then-act; the race it
        # cannot close is closed by the claim itself.
        occupant = self._dispatcher.holder(room.id)
        if occupant is not None and occupant != wc.name:
            raise RoomAlreadyRoutedError(
                f"Room '{wc.room}' is already served by watcher '{occupant}', so "
                f"watcher '{wc.name}' cannot also take it: both would answer every "
                f"message, and neither would see the other's reply. Give the second "
                f"watcher its own connector (its own bot account) or its own room."
            )

        # 2. Provision session. The identity is computed once and both compared and
        # stored below, so the value a later run compares against is the same string
        # this run validated — not a second derivation that could drift from it.
        identity = backend_identity(agent_cfg.type, agent_cfg.working_directory)
        session_id, created_new_session = await self._provision_session(
            wc, state, agent, agent_cfg, identity, room.id
        )
        if created_new_session and state and state.session_id:
            # The record survived but its session did not, so the bookkeeping keyed to
            # the old id has to go with it — the same pairing `reset_watcher` makes when
            # it clears a session id. Without this the retry counter for the abandoned
            # session outlives it, and a watcher that had reached failed_degraded would
            # carry that verdict into a session which has never been injected at all.
            self._injector.reset_session(state.session_id)

        # 3. Build state and register maps
        ws = WatcherState(
            watcher_name=wc.name,
            session_id=session_id,
            room_id=room.id,
            room_type=room.type,
            # The resolved room's own name, so that `list` — which reads records,
            # not config (§2.8) — can name the room without going back to the
            # config entry that no longer exists under rule-derived watchers.
            room_name=room.name,
            # False whenever the session is new, not merely when the record is:
            # the flag describes what a *session* has received, and a replacement
            # session has received nothing. `reset_watcher` already pairs "clear the
            # session id" with "clear this flag"; an identity mismatch replaces the
            # session without going through that path, so it has to pair them too.
            context_injected=(
                state.context_injected
                if state and not created_new_session
                else False
            ),
            paused=False,
            last_processed_ts=state.last_processed_ts if state else "",
            backend_identity=identity,
            # Applied here, at construction, so the record is never observable
            # in a state that has a session but no provenance. Unknown keys are
            # refused rather than ignored: a typo'd field name would otherwise
            # silently mean "this field was not carried", which is the exact
            # failure mode this parameter exists to close.
            **(provenance or {}),
        )
        self._states[wc.name] = ws
        try:
            self._maps.bind_session(session_id, room.id, self._connector)
        except Exception:
            # A refused binding must not leave this watcher looking startable. Without
            # this, the record just written stays in `_states`, `sync_watchers` persists
            # it, and the next boot's uniqueness preflight refuses to start at all — a
            # transient conflict turned into a permanently unbootable state file. The
            # freshly created backend session goes too; nothing will ever use it.
            self._states.pop(wc.name, None)
            await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            raise

        # 3.5 Fetch channel history for context handoff (new sessions only).
        # Only fires when created_new_session=True (reset, upgrade, first join)
        # so that resumed sessions (same session ID) are not re-injected.
        # Failure is non-fatal: a failed fetch logs a warning and the watcher
        # starts without history rather than blocking the entire startup.
        history_context: str | None = None
        hh = wc.history_handoff
        if created_new_session and hh.enabled:
            try:
                raw_msgs = await self._connector.fetch_room_history(
                    room, hh.fetch_count, before_ts=history_before_ts
                )
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
                watcher_name=wc.name,
                # The prompt file is per WATCHER, not per room. The collision it was
                # written for — two watchers with different agents in one room — is now
                # refused (§4.1), but the key still names files on disk and re-keying
                # them would orphan every existing one for no gain. See
                # watcher_prompt_key for why this is not room_path_key.
                path_key=watcher_prompt_key(wc.connector, room.id, wc.name),
                content=built_content,
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
                room_path_key(wc.connector, room.id),
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

        # 7-8. Subscribe, then claim the room — one rollback covers both.
        #
        # They were separate, and step 8 had no failure path at all: a refused claim
        # (the room is another watcher's) left the room subscribed and the session bound
        # with nothing to answer, which reads as a healthy watcher receiving nothing.
        # The claim is what makes a watcher live, so it belongs inside the same undo as
        # the subscription that feeds it.
        subscribed = False
        try:
            await self._connector.subscribe_room(
                room,
                watcher_id=wc.name,
                working_directory=agent_cfg.working_directory,
            )
            subscribed = True
            self._dispatcher.add_processor(room.id, processor)
        except Exception:
            if subscribed:
                try:
                    await self._connector.unsubscribe_room(room.id, watcher_id=wc.name)
                except Exception as unsub_error:  # best effort; the raise below is what matters
                    logger.warning(
                        "Watcher '%s': could not unsubscribe room '%s' while rolling "
                        "back a failed start: %s",
                        wc.name,
                        room.id,
                        unsub_error,
                    )
            self._processors.pop(wc.name, None)
            # Keep ws in _states (do NOT pop) so that the context_injected flag
            # and session_id are preserved for the next start.
            cleaned = await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            if cleaned and created_new_session:
                ws.session_id = ""
                # The session that received context injection was destroyed, so
                # the next start will create a brand-new session that
                # has never seen the context.  Reset the flag so injection is
                # re-attempted for the new session — without this, the new
                # session inherits context_injected=True from the old ws and
                # the agent silently operates without its system context.
                ws.context_injected = False
            self._states[wc.name] = ws
            self._maps.remove_session(session_id)
            raise

        # 9. Activate processor — starts the consumer loop and emits the
        # online notification.  Deferred to here so users never see "online"
        # for a watcher whose room subscription failed.
        processor.start()

        # 10. Restore watermark.
        # Only advance the room-level watermark; never move it backwards.
        # A watcher that was paused or reset may hold an older persisted timestamp
        # than the room's live cursor — the connector keeps that cursor per room and
        # advances it as messages are accepted, independently of any one watcher's
        # restarts. Writing the older value back would redeliver everything between
        # the two after the next reconnect. (This used to be justified by sibling
        # watchers sharing a room, which §4.1 no longer permits; the reason above is
        # the one that still holds.)
        #
        # The third site of one rule, and the only one that reads rather than writes. The
        # decision has a name because it has three answers and I got it wrong twice while
        # it was spelled out inline — see `_should_restore_watermark`.
        current_ts = self._connector.get_last_processed_ts(room.id)
        if _should_restore_watermark(ws.last_processed_ts, current_ts):
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
        identity: str,
        room_id: str,
    ) -> tuple[str, bool]:
        """Determine the session ID: reuse the persisted one, or create a new one.

        Priority:
          1. Persisted ``state.session_id``, **if it was created against this same
             backend identity** (§2.4).
          2. Create a new session via the agent backend.

        There is no config-pinned option: `watchers[].session_id` is removed, so
        every session id the gateway uses is one it assigned itself.

        **The room is compared as well as the identity**, and this is the reachable half.
        Editing `room:` on an existing watcher is ordinary reconfiguration, and the
        record survives it — so without this the old session is rebound to the new room,
        carrying that room's transcript and identity header into it, and the watermark
        restore then writes the *old* room's cursor onto the new room, silently
        discarding every message in it older than that timestamp. The state-file
        corruption both error messages tell operators to look for is far rarer than this.
        An empty `room_id` is a **mismatch**, not "no claim to compare". `room_id` has no
        dataclass default, so it is in `_REQUIRED_FIELDS` and the reader fills an absent
        one with `""` — meaning an empty value in a version-2 record is a hand-edited or
        truncated record, which is precisely the case this refusal exists for. Treating
        it as matching would resume an unknown room's transcript in this room. This began
        as an exemption for "a record written before the field carried anything", which
        was wrong twice over: no such record can be read (the format version refuses
        them), and it contradicts the rule the identity comparison already follows —
        unverifiable is not verified.

        A session id is only meaningful inside the backend store that issued it, so a
        record whose stored identity does not equal the current one is not reused —
        replaying the id into a different store loses continuity silently, or matches an
        unrelated session carrying the same id. An **empty** stored identity is treated
        the same way, and that is the permanent rule rather than a migration allowance:
        the field defaults to `""` and is not required, so a v2 file that omits it reads
        as empty long after this branch ships. Both cases answer "can this id be verified
        as belonging to this store?" with no, and unverifiable is not verified.

        The abandoned id is **not** deleted. Deletion would run against the *current*
        backend, where that id either means nothing or means someone else's session —
        the precise confusion this comparison exists to avoid. It is dropped, and the
        fresh session takes over; the old store keeps whatever it had.
        """
        if state and state.session_id:
            if state.backend_identity == identity and state.room_id == room_id:
                return state.session_id, False
            # The full id, deviating from the [:8] used for routine session logging.
            # This record is about to be overwritten with the new session, so this line
            # is the only place the abandoned id survives — and the one use it has left
            # is being pasted into the backend's own resume command.
            logger.warning(
                "Watcher '%s': not reusing session %s — %s. Starting a fresh session; "
                "the previous conversation stays in the backend it was created against "
                "and can be resumed there by hand with this id. "
                "Expected after changing an agent's type or working_directory.",
                wc.name,
                state.session_id,
                (
                    f"it belongs to room '{state.room_id}' and this watcher now "
                    f"watches '{room_id}'"
                    if state.backend_identity == identity
                    else f"it was created against backend identity "
                         f"'{state.backend_identity}', which is now '{identity}'"
                    if state.backend_identity
                    else f"it has no recorded backend identity to check against "
                         f"'{identity}'"
                ),
            )
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

    async def _stop_processor(self, name: str) -> None:
        """Stop a processor and clean up all mappings.

        Order is critical for correctness:
          1. Remove from dispatcher — new inbound messages stop being routed here.
          2. Capture live watermark — MUST precede the unsubscribe, see below.
          3. Unsubscribe from connector — transport stops delivering for this room.
          4. Stop the processor — drains any already-queued messages, then shuts down.
          5. Clean session maps.

        Why capture comes before unsubscribe: unsubscribing the *last* watcher on
        a room pops the connector's per-room state (``self._rooms`` on
        Rocket.Chat, ``self._channels`` on Mattermost), and the watermark lives
        in exactly that entry.  Reading it afterwards returned None — silently,
        because ``dict.get`` does not raise, so the copy below simply never
        fired and the stale value was persisted.  On restart every message
        between the stale watermark and the true one was redelivered.

        This capture used to sit after the drain, on the reasoning that the
        timestamp should reflect the last message the processor *actually*
        handled.  That reasoning does not hold: both connectors advance their
        watermark when a message is **accepted into the queue**
        (``rocketchat/connector.py`` and ``mattermost/connector.py``, at the
        point ``dispatch()`` confirms acceptance), not when it is handled.  Step
        1 has already removed this processor from the dispatcher, so
        ``dispatch()`` finds none and returns False, and the connector cannot
        advance the watermark any further.  The value is therefore identical
        before and after the drain — and capturing before the unsubscribe is the
        only position where it is readable at all.

        Capture is deliberately *not* moved after the drain-and-before-unsubscribe
        instead, which would also read the right value: that would leave the room
        subscribed for the whole drain, widening the window in which arriving
        messages find no processor and are dropped by the dispatcher.
        """
        processor = self._processors.pop(name, None)
        state = self._states.get(name)
        errors: list[str] = []

        # Step 1: Remove from dispatcher so no new messages are routed to this processor.
        if processor and state and state.room_id:
            self._dispatcher.remove_processor(state.room_id, processor)

        # Step 2: Capture the live watermark while the connector still holds the
        # room entry it lives in — the unsubscribe below pops that entry.
        # Best-effort (#118 defect 3): the same read is already best-effort in
        # StateStore.save, and a raise here used to abandon the unsubscribe
        # and the drain — leaving the room subscribed and the queue undrained —
        # in order to preserve a watermark that then was not captured anyway.
        if state and state.room_id:
            try:
                live_ts = self._connector.get_last_processed_ts(state.room_id)
            except Exception as e:
                live_ts = None
                logger.warning(
                    "Watcher '%s': could not read the live watermark during "
                    "stop (proceeding; the persisted mark stands): %s", name, e,
                )
            # `is not None`, so a connector that has *cleared* its watermark can say so.
            # `None` still means "no opinion — this room saw no activity in this run", and
            # must not erase what is on disk. An empty string is an opinion: a connector
            # that learned this account is no longer in the room clears the mark precisely
            # so a later re-add cannot replay the interval it was absent for, and a save
            # that skipped it would hand that mark straight back on the next start.
            if live_ts is not None:
                state.last_processed_ts = live_ts

        # Step 3: Unsubscribe from the connector (stop delivery for this room).
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

        # Step 4: Stop the processor (drain the queue; _stopping=True rejects late arrivals).
        if processor:
            try:
                await processor.stop()
            except Exception as e:
                errors.append(f"processor stop failed: {e}")
                logger.error("Watcher '%s': processor stop failed: %s", name, e)

        # Step 5: Clean up session maps.
        # Convention: empty string "" in session_id means "no session" (not yet
        # assigned).  The falsy guard below skips cleanup in that case.
        if state:
            effective_session = state.session_id
            if effective_session:
                if self._permission_registry:
                    self._permission_registry.cancel_session(effective_session)
                self._maps.remove_session(effective_session)

        # Persisting is the caller's job.  Every caller already saves at a point
        # that suits it — pause_watcher and reset_watcher after their own state
        # mutations, stop_all via save_state() during shutdown — so a `save`
        # flag here was dead in all three cases and has been removed rather than
        # left as an option nothing selects.
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
