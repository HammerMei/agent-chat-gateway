"""SessionManager: thin orchestrator that wires collaborators together.

Delegates all real work to focused collaborators:
  - WatcherLifecycle: start/stop/pause/resume/reset watchers
  - MessageDispatcher: inbound message routing + permission interception
  - InjectedContextBuilder: context file reading + durable agent session delivery
  - StateStore: WatcherState persistence + watermark management
  - SessionMaps: shared session→room/role/connector routing state
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime

from ..agents import AgentBackend
from .config import CoreConfig
from .connector import (
    Connector,
    IncomingMessage,
    MembershipHook,
    Room,
    User,
    UserRole,
)
from .dispatch import MessageDispatcher
from .injected_context_builder import InjectedContextBuilder
from .lifecycle_sweep import LifecycleSweep
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import (
    StateFilter,
    parse_state_filter,
    past_idle_ttl,
    room_kind_or_channel,
)
from .state_store import StateStore
from .watcher_lifecycle import WatcherLifecycle
from .watcher_manager import RoomRef, WatcherManager
from .watcher_rule import RoomKind

logger = logging.getLogger("agent-chat-gateway.core.session_manager")


# The shared degrade-don't-raise conversion lives in state.py since Codex
# round 9 found a THIRD raising site — one helper, every consumer.
_room_kind_or_channel = room_kind_or_channel


class SessionManager:
    """Thin orchestrator: wires collaborators and manages top-level lifecycle.

    Accepts any Connector implementation — RocketChatConnector, ScriptConnector,
    or future Slack/Discord connectors — without knowing their platform details.

    Usage::

        manager = SessionManager(connector, agents, "assistance", core_config,
                                 watcher_rules=rules)
        await manager.run()   # blocks until cancelled
    """

    def __init__(
        self,
        connector: Connector,
        agents: dict[str, AgentBackend],
        default_agent: str,
        config: CoreConfig,
        state_name: str = "default",
        permission_registry: PermissionRegistry | None = None,
        session_maps: SessionMaps | None = None,
        watcher_rules: list | None = None,
        pending_jobs=None,
        cancel_jobs=None,
    ) -> None:
        self._connector = connector
        # `state_name` is the connector's config name in production (service.py
        # passes `cc.name`), which is also the name rules bind to — the manager
        # and the router closure below both key on it.
        self._connector_name = state_name
        maps = session_maps or SessionMaps()

        # Collaborators
        self._dispatcher = MessageDispatcher(connector, permission_registry)
        self._injector = InjectedContextBuilder(config)
        self._state_store = StateStore(state_name, connector)
        self._lifecycle = WatcherLifecycle(
            connector=connector,
            agents=agents,
            default_agent=default_agent,
            config=config,
            state_store=self._state_store,
            dispatcher=self._dispatcher,
            injector=self._injector,
            permission_registry=permission_registry,
            maps=maps,
        )
        # The manager is constructed UNCONDITIONALLY, empty rule list and all
        # (Codex round 5, P1). It was once gated on rules existing — a
        # pre-cutover rule protecting static-only deployments' delivery
        # behaviour — but that deployment shape no longer loads, and the gate
        # had acquired a new victim: a connector whose operator removed its
        # last rule still hydrates its self-sufficient rule-derived records
        # (§2.4 keeps them until expiry), and without a manager those records
        # got no router, no boot recreation, no replay and no sweep — every
        # existing session unreachable, silently. With an empty rule list the
        # manager recreates persisted records and merely declines genuinely
        # new rooms. Consequence, named: the router is now always registered,
        # so Rocket.Chat runs subscribe-all even with zero rules — every
        # offer declined, which is the uniform §2.8 behaviour post-cutover.
        self._watcher_manager = WatcherManager(
            state_name, connector, self._lifecycle,
            # Normalized like `_watcher_rules` below (Codex round 10): a
            # standalone caller using the declared default None would hand
            # the always-on manager an un-iterable rule list, and the first
            # new room's first_matching_rule would raise instead of declining.
            list(watcher_rules or []))
        # The idle sweep exists only where the transport can wake what it
        # drops: §2.6 rules eager connectors (Script, Voice) "never"
        # idle-eligible — no message can ever arrive to wake an idled room
        # there, so a timer that dropped one would be muting it permanently.
        self._sweep = (
            LifecycleSweep(self._lifecycle, pending_jobs=pending_jobs,
                           reconcile=self._reconcile_membership)
            if connector.supports_unsolicited_inbound()
            else None
        )
        # Fired by the membership-remove handler for the reclaimed watcher's
        # name: its pending jobs are cancelled with a stated reason rather
        # than left pointing at nothing (§2.7). Injected like `pending_jobs`,
        # because the job store lives above this layer.
        self._cancel_jobs = cancel_jobs
        # Kept for the eager-start loop (§2.6): a connector with no
        # unsolicited inbound never has a room offered to it, so its rules'
        # literal rooms are walked at boot instead.
        self._watcher_rules = list(watcher_rules or [])

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, sync watchers, block until cancelled.

        Note: control socket ownership belongs to GatewayService.
        Use run_once() when GatewayService is the orchestrator (normal production use).
        This method is kept for standalone/test use cases that don't use GatewayService.
        """
        await self.run_once()
        logger.info("SessionManager running")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_once(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Connect and sync watchers WITHOUT blocking forever.

        Args:
            unavailable_agents: Optional set of agent names whose permission
                broker failed to start.  Watchers that use these agents will
                be skipped with an error rather than started without permission
                enforcement.

        Returns:
            List of human-readable error strings for any watchers that failed.
        """
        await self.connect_only()
        errors = await self.sync_only(unavailable_agents=unavailable_agents)
        logger.info("SessionManager ready (run_once)")
        return errors

    async def connect_only(self) -> None:
        """Phase one: authenticate. No subscription, no watcher, no room.

        Split from `sync_only` so an orchestrator holding several connectors can put a
        barrier between the phases — two connectors on one bot account have to be
        refused *before* either subscribes, and each SessionManager owns exactly one
        connector, so nothing at this layer can see the collision (§4.5).

        A single-connector caller has no such barrier to run and should keep using
        `run_once()`.
        """
        self._connector.register_handler(self._dispatcher.dispatch)
        self._connector.register_capacity_check(self._dispatcher.capacity)
        if (self._watcher_manager is not None
                and self._connector.supports_unsolicited_inbound()):
            # The discovery hooks belong to discovering connectors only:
            # Script and Voice have no stream to offer a room from — their
            # rooms start eagerly (§2.6) — and neither implements
            # register_router. Before start_inbound(), necessarily:
            # Rocket.Chat's start_inbound attempts subscribe-all only when a
            # router is already registered, so a router registered later
            # would never receive an offer.
            self._connector.register_router(self._route_unclaimed_room)
            # Alongside the router, and gated the same way: membership events
            # exist to serve the rule-derived lifecycle, and a static-only
            # deployment keeps its exact behaviour until its operator writes a
            # rule. The base method is a no-op, so this is safe on a connector
            # with no membership stream.
            self._connector.register_membership_hook(MembershipHook(
                added=self._on_membership_added,
                removed=self._on_membership_removed,
            ))
        await self._connector.connect()

    async def sync_only(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Phase two: restore watchers, subscribe, then open the inbound stream.

        `start_inbound()` is last on purpose. A connector that delivers everything its
        account can see discards events for rooms it has no state for, so reading before
        the restore turns "not subscribed yet" into "lost" — and the errors returned
        here are per-watcher failures, not a reason to leave the connector deaf, so the
        stream starts regardless of them.
        """
        errors = await self._lifecycle.sync_watchers(unavailable_agents=unavailable_agents)
        # Eager creation for connectors with no unsolicited inbound (§2.6):
        # nothing ever offers them a room, so their rules' literal rooms are
        # started here, before the inbound surface opens.
        await self._eager_start_rule_rooms(errors)
        # Snapshotted before the stream opens, and that placement is the whole
        # defence against the race below: after `start_inbound` a live message
        # can recreate a room and advance its watermark past the entire
        # down-window before the replay loop reaches that record. Reading the
        # watermarks here freezes what "missed" means; the replay itself still
        # runs after the stream, so a room's live traffic is never held up by
        # a REST probe.
        down_window = self._snapshot_watermarks()
        await self._connector.start_inbound()
        # Before the replay, deliberately: a record this evaluation recreates
        # owns its own replay (§2.2, replay ownership), so the loop below finds
        # it resident and skips it — and a record it marks idle is still probed
        # below, because messages waiting in a room outrank its idleness.
        await self._evaluate_lifecycle_at_boot()
        await self._replay_persisted_records(down_window)
        if self._sweep is not None:
            # After the replay completes, structurally — §2.5 makes this
            # ordering non-optional once the expiry leg exists (expiry deletes
            # the record recreation reads from, and the replay's whole job is
            # reviving rooms with messages waiting), and the idle leg honors it
            # from day one so step 5 does not have to move this line.
            self._sweep.start()
        return errors

    async def _eager_start_rule_rooms(self, errors: list[str]) -> None:
        """Start every literal rule room on a connector that cannot discover (§2.6).

        Script's messages arrive by direct injection and Voice's rooms as HTTP
        path segments — no stream ever offers a room, so lazy creation can
        never fire. Their rules name **literal** rooms (enforced at config
        load, `_enforce_literal_rooms`), and each named room starts at boot
        through `get_or_create`: sticky binding, the paused refusal and the
        §5.3 record fields all apply exactly as they do on the message path.
        `resolve_room(name)` here is the one legitimately name-based
        resolution left (§2.8) — an eager rule genuinely starts from a name.

        Per-room failures append to the startup error list, as the static
        loop's did; boot proceeds, and the daemon reports the list at startup.
        **The catch below deliberately narrows `get_or_create`'s contract**
        ("an exception is retryable — the caller owns the retry"): on an
        eager connector no message can ever re-trigger the room, so the only
        retries that exist are the next daemon restart and the operator's
        `resume`. A room whose eager start fails therefore stays down, loudly,
        until one of those — a weaker recovery than RC/MM's redelivery, and
        the price of a transport with no inbound stream. Connectors with
        unsolicited inbound return immediately — their rooms arrive, they are
        never walked.
        """
        if self._watcher_manager is None:
            return
        if self._connector.supports_unsolicited_inbound():
            return
        kind_for = {k.value: k for k in RoomKind}
        # Rooms the rule loop already attempted — the record walk below must
        # not retry them: a start that failed loudly there would otherwise be
        # attempted (and reported) twice in one boot.
        attempted: set[str] = set()
        for rule in self._watcher_rules:
            for pattern in rule.rooms.include:
                if not pattern.is_literal:
                    # Load enforcement makes this unreachable; skipping is
                    # belt-and-braces, not a policy.
                    continue
                name = pattern.raw
                if any(p.matches(name) for p in rule.rooms.except_for):
                    # The rule's own veto (Codex round 3): an included literal
                    # that except_for also matches is DECLINED by the same
                    # rule, and get_or_create would correctly answer None —
                    # which the branch below would then misreport as a
                    # startup failure on every boot. An intentional exclusion
                    # is a no-op, not an error.
                    logger.info(
                        "Rule '%s': room '%s' is excluded by the rule's own "
                        "except_for — not started", rule.name, name,
                    )
                    continue
                try:
                    room = await self._connector.resolve_room(name)
                    ref = RoomRef(
                        id=room.id,
                        kind=kind_for.get(room.type, RoomKind.CHANNEL),
                        name=room.name,
                    )
                    attempted.add(ref.id)
                    proc = await self._watcher_manager.get_or_create(
                        self._connector_name, ref)
                except Exception as e:
                    msg = (f"Rule '{rule.name}' (room '{name}'): eager start "
                           f"failed: {e}")
                    logger.error(msg)
                    errors.append(msg)
                    continue
                if proc is None:
                    record = self._lifecycle.record_for_room(ref.id)
                    if record is not None and record.paused:
                        logger.info(
                            "Rule '%s': room '%s' is paused — not started",
                            rule.name, name,
                        )
                    else:
                        msg = (f"Rule '{rule.name}' (room '{name}'): eager "
                               f"start produced no watcher — check that the "
                               f"room matches the rule's include patterns")
                        logger.error(msg)
                        errors.append(msg)
        # The persisted records, independently of the current rules (Codex
        # round 6, completing round 5's unconditional manager): sticky binding
        # (§2.4) keeps a record alive after its rule is deleted or renamed,
        # and on an eager connector no message can ever arrive to wake it —
        # the rule loop above is the only starter there is. Rooms the rule
        # loop just started answer with their resident processor; paused
        # records are skipped silently (an operator's mute, not a failure);
        # anything else failing to start is loud, per the eager contract.
        for record in list(self._lifecycle.states().values()):
            if not record.config or not record.room_id:
                continue
            if record.room_id in attempted:
                # The rule loop already attempted this room this boot — a
                # failure there was reported once, loudly, and a second
                # attempt here would double it.
                continue
            if self._lifecycle.processor_named(record.watcher_name) is not None:
                continue
            if record.paused:
                logger.info(
                    "Record '%s': room %s is paused — not started",
                    record.watcher_name, record.room_id,
                )
                continue
            ref = RoomRef(
                id=record.room_id,
                kind=kind_for.get(record.room_kind, RoomKind.CHANNEL),
                name=record.room_name,
                participants=tuple(record.participants),
            )
            try:
                proc = await self._watcher_manager.get_or_create(
                    self._connector_name, ref)
            except Exception as e:
                msg = (f"Record '{record.watcher_name}' (room "
                       f"'{record.room_id}'): eager recreation failed: {e}")
                logger.error(msg)
                errors.append(msg)
                continue
            if proc is None:
                msg = (f"Record '{record.watcher_name}' (room "
                       f"'{record.room_id}'): eager recreation produced no "
                       f"watcher")
                logger.error(msg)
                errors.append(msg)

    async def _evaluate_lifecycle_at_boot(self) -> None:
        """Boot runs the same evaluation the sweep runs (§2.5), over the
        records that were *active* at shutdown.

        One function, two callers: the TTL arithmetic is `past_idle_ttl`, the
        same call the sweep makes — a boot rule and a running rule would drift
        on exactly the restart-only path nothing exercises.

        A was-active record (rule-derived, not paused, no `dropped_at`) is
        either **recreated** through `get_or_create` — so replay, sticky
        binding and the paused refusal all apply for free — or, when it is
        already past its idle TTL, **marked idle with a fresh `dropped_at`**
        rather than paying a resume for a room the first sweep would drop
        minutes later. Fresh, never backdated: expiry measures from this
        moment, which is what keeps `active → expired` impossible through an
        outage of any length (§2.5).

        This is also what keeps `list` honest after a restart: a was-active
        record has no `dropped_at` and no processor, which reads as `failed` —
        the default view reporting a healthy fleet as broken. After this pass
        every such record is resident again or honestly idle.
        """
        if self._watcher_manager is None:
            return
        if not self._connector.supports_unsolicited_inbound():
            # §2.6: eager connectors are never idle-eligible — a record this
            # evaluation stamped idle could never be woken (no message can
            # arrive), so it would be muted permanently. Their records were
            # just restarted by the eager loop instead.
            return
        now = datetime.now().astimezone()
        stamped = 0
        for record in list(self._lifecycle.states().values()):
            if (
                not record.rule_name
                or record.paused
                or record.dropped_at
                or not record.room_id
            ):
                continue
            if past_idle_ttl(record, now):
                record.dropped_at = now.isoformat(timespec="seconds")
                stamped += 1
                logger.info(
                    "Watcher '%s' was idle past its TTL across the restart — "
                    "marked idle; its next message wakes it",
                    record.watcher_name,
                )
                continue
            # Tolerant, not raising (Codex round 8): `load_state` promises to
            # degrade a corrupted record rather than let one take the service
            # down, and a raising enum conversion OUTSIDE the per-record try
            # defeated that contract — one garbled room_kind aborted the
            # whole connector's boot. Unknown falls back to CHANNEL, loudly:
            # the mention gate applies there, which is the safe default.
            kind = _room_kind_or_channel(record)
            try:
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=record.room_id,
                        kind=kind,
                        name=record.room_name,
                        participants=tuple(record.participants),
                    ),
                    # The snapshot pin (Codex round 11): a live removal can
                    # reclaim this record mid-walk, and an unpinned call
                    # would _create a fresh watcher for the just-left room.
                    expected_record=record,
                )
            except Exception as e:
                # An abort, not a decision (§2.2): the record keeps reading
                # `failed`, which is the honest state, and the next boot — or
                # the room's next message — retries.
                logger.warning(
                    "Boot recreation failed for watcher '%s' (room %s) — it "
                    "stays failed until its next message or the next start: %s",
                    record.watcher_name, record.room_id, e,
                )
        if stamped:
            self._lifecycle.save_state()

    def _snapshot_watermarks(self) -> dict[str, str]:
        """Each rule-derived record's watermark as of boot, by watcher name.

        Taken after `sync_watchers` — hydration is what puts the records in
        memory — and before the inbound stream opens, so every value is the
        boundary of the interval the daemon was down for, uncontaminated by
        anything that arrives after.
        """
        return {
            ws.watcher_name: ws.last_processed_ts
            for ws in self._lifecycle.states().values()
            if ws.rule_name and ws.last_processed_ts
        }

    async def _replay_persisted_records(self, down_window: dict[str, str]) -> None:
        """Recover messages that arrived while the daemon was down (§2.2).

        The abort guarantee — "watermark unchanged, so redelivery can retry" —
        is only worth something if something actually redelivers, and at
        startup nothing did: both connectors replay from their *reconnect*
        callback, which a process restart never fires. This walks the
        persisted rule-derived records instead, and for each one probes the
        gap between its boot-time watermark and now. An empty gap leaves the
        room idle — that is the lazy model working. A non-empty gap recreates
        the watcher from its own record (sticky, §2.4 — rules are not
        consulted) and replays the window through the connector's normal
        pipeline, where the restored watermark and the id window dedup as
        usual.

        ``down_window`` holds the watermarks as of *before the stream opened*
        (`_snapshot_watermarks`), and every decision here reads it rather than
        the record. A live message arriving between `start_inbound` and this
        loop recreates its room and advances that record's watermark past the
        whole down-window; reading the record then would report no gap and skip
        the room, losing every message from the outage with no log line. The
        snapshot also means a room that is *already resident* by the time the
        loop reaches it is still replayed — being resident is not evidence that
        anyone looked below the boundary.

        Best-effort per record: a room whose probe, recreation or replay fails
        stays idle, and its next live message triggers a recreation — which
        replays from the record's watermark, so the interval is recovered
        rather than merely the room. Boot must not die on one bad room.

        The accepted residual, restated: a room that never produced a record —
        or produced one with no watermark — has nothing to replay from, and a
        message that arrived for it while the daemon was down is gone until
        someone speaks again.
        """
        if self._watcher_manager is None:
            return
        if not self._connector.supports_unsolicited_inbound():
            # §2.6: an eager connector has no history surface to probe and no
            # down-window to recover — its messages arrive by injection or
            # HTTP request, which nothing buffers while the daemon is down.
            return
        for ws in list(self._lifecycle.states().values()):
            boundary = down_window.get(ws.watcher_name)
            if not boundary:
                # No record at boot, or no watermark to replay from — including
                # every room created live since the stream opened, which has
                # nothing behind it to recover.
                continue
            if ws.paused or not ws.config or not ws.room_id:
                continue
            room = Room(
                id=ws.room_id,
                name=ws.room_name or ws.watcher_name,
                # The record's kind, so a group DM reaches the direct-room
                # history endpoint rather than a channel one.
                type=ws.room_kind or ws.room_type or "channel",
            )
            try:
                missed = await self._connector.probe_missed_since(room, boundary)
            except Exception as e:
                logger.warning(
                    "Startup replay: could not probe room %s for watcher '%s': %s",
                    ws.room_id, ws.watcher_name, e,
                )
                continue
            if not missed:
                continue
            try:
                if self._lifecycle.processor_named(ws.watcher_name) is not None:
                    # Already resident, so a recreation already ran for it — and
                    # a recreation owns its room's replay (§2.2, "replay
                    # ownership"). Nothing to do: replaying here would be a
                    # second pass over the interval the recreation just covered,
                    # and a second pass is not free, because replay hands the
                    # filter its own boundary rather than the live watermark —
                    # so the ts filter suppresses nothing and only the bounded
                    # id window separates the two.
                    #
                    # That a resident room with a record must have come through
                    # a recreation is not an assumption: its record rules out
                    # `_create`, and a rule-derived record is absent from
                    # `watchers:`, so the static path never starts one either.
                    continue
                # Tolerant like the boot and injection paths (Codex round 9 —
                # the third raising site): a garbled kind must not strand the
                # outage messages behind an idle record no live traffic wakes.
                kind = room_kind_or_channel(ws)
                # Triggering the recreation is this loop's whole job. It owns no
                # interval of its own; the recreation replays what the room owes.
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=ws.room_id,
                        kind=kind,
                        name=ws.room_name,
                        participants=tuple(ws.participants),
                    ),
                    # The snapshot pin (Codex round 11) — same as the boot
                    # evaluation's: this loop walks records hydrated before
                    # the inbound stream opened, and a live removal can
                    # reclaim one mid-walk.
                    expected_record=ws,
                )
            except Exception as e:
                logger.warning(
                    "Startup replay failed for watcher '%s' (room %s) — the room "
                    "stays idle; its next message recreates it and replays from "
                    "the record's watermark, so this interval is deferred rather "
                    "than lost: %s",
                    ws.watcher_name, ws.room_id, e,
                )

    async def _route_unclaimed_room(self, room: RoomRef, trigger) -> None:
        """The router the connectors call for a room no watcher tracks (§2.2).

        The manager answers the whole question — sticky record, rule match,
        create or drop — and the connector delivers the trigger afterwards if
        the room became tracked. Nothing here inspects the trigger beyond
        asking the connector what history bound it implies: the frame is
        platform-shaped, and this layer deliberately is not.
        """
        await self._watcher_manager.get_or_create(
            self._connector_name,
            room,
            history_before_ts=self._connector.trigger_history_bound(trigger),
        )

    async def _on_membership_added(self, room: RoomRef) -> None:
        """The bot was added to a room: register its record idle (§2.7).

        Never raises toward the connector — an add the handler drops is
        re-discovered by the room's first message, which is the safety net
        membership registration supplements and never replaces.
        """
        if self._watcher_manager is None or self._watcher_manager.disarmed:
            return
        try:
            await self._watcher_manager.register_on_join(room)
        except Exception:
            logger.exception(
                "Membership-add handling failed for room %s — the room's "
                "first message still creates its watcher", room.id,
            )

    async def _on_membership_removed(self, room_id: str) -> None:
        """The bot was removed from a room: reclaim its record, cancel its jobs.

        Never raises toward the connector — a remove the handler drops is
        re-discovered by the periodic membership reconciliation, and
        `reclaim_room` is idempotent so the late discovery reaches the same
        end state. Gated on disarm like the add: a reclaim racing stop_all
        would dismantle state the shutdown is flushing.
        """
        if self._watcher_manager is None or self._watcher_manager.disarmed:
            return
        # Counted in the shutdown barrier (Codex round 10): a removal past
        # the gate above but parked on the watcher lock or mid-reclaim was
        # invisible to both drains — the final save could run mid-prune, and
        # disconnect's task cleanup then cancelled the reclamation half-done.
        # The traced residue was bounded (record popped last, reconciliation
        # re-reclaims), but the barrier machinery makes counting it three
        # lines. The reconciliation's calls stay uncounted on purpose: they
        # ride the sweep task, which shutdown cancels and awaits first.
        try:
            self._lifecycle._enter_verb("reclaim", room_id)
        except RuntimeError:
            return  # disarm flipped since the gate above — same answer.
        try:
            await self._reclaim_removed_room(
                room_id,
                reason="the platform reported the bot removed from the room",
            )
        finally:
            self._lifecycle._exit_verb()

    async def _reclaim_removed_room(
        self, room_id: str, *, reason: str, expected=None,
        require_dormant: bool = False,
    ) -> None:
        """The removal path's shared tail: reclaim the record, cancel its jobs.

        Two discoverers, one end state (§2.7): the live membership event and
        the periodic reconciliation both land here, so a removal discovered
        late cannot reach a different outcome than one seen live.
        """
        try:
            name = await self._lifecycle.reclaim_room(
                room_id, reason=reason, expected=expected,
                require_dormant=require_dormant)
        except Exception:
            logger.exception(
                "Membership-removal reclaim failed for room %s — the "
                "reconciliation re-discovers it", room_id,
            )
            return
        if name is not None and self._cancel_jobs is not None:
            try:
                self._cancel_jobs(name)
            except Exception:
                logger.exception(
                    "Could not cancel scheduled jobs for reclaimed watcher "
                    "'%s' — they will fail audibly when they fire", name,
                )

    async def _reconcile_membership(self) -> None:
        """The periodic membership reconciliation (§2.7): the backstop for a
        missed removal event, for exactly the records nothing else can reach.

        "Discovered later as a REST failure" needs a future operation to touch
        the room, and a **dormant** record — paused or idle — has no timer
        reclamation and receives no inbound, so nothing ever touches it: a
        missed remove leaves its session, prompt file, attachment directory
        and jobs alive indefinitely, and `resume` keeps offering a room the
        bot cannot access (R3 by another route). Active records are excluded
        because the live paths do reach them; static records because their
        owner is config.yaml.

        Keyed on the connector declaring unsolicited inbound, not on a
        platform: Rocket.Chat's reconnect replay covers message history, not
        membership, so it has the same hole for a paused room. **Fail means
        keep**: a snapshot that could not be read (None) reclaims nothing —
        only an answered snapshot that unambiguously omits the room does.
        """
        if self._watcher_manager is None or self._watcher_manager.disarmed:
            return
        # A method, not a property — the unparenthesised form is a bound
        # method and always truthy, which silently killed this gate once.
        if not self._connector.supports_unsolicited_inbound():
            return
        dormant = [
            r for r in self._lifecycle.states().values()
            if (r.paused or r.dropped_at) and r.config and r.room_id
        ]
        if not dormant:
            return
        snapshot = await self._connector.membership_snapshot()
        if snapshot is None:
            logger.info(
                "Membership snapshot unavailable — reconciliation kept all "
                "%d dormant record(s) this round", len(dormant),
            )
            return
        for record in dormant:
            if record.room_id in snapshot:
                continue
            # Re-read before acting (Codex review of #121): the snapshot ages
            # while this loop awaits earlier reclamations, and both halves of
            # staleness are real — a wake can make the record active again
            # (dropped_at cleared), and a re-add can replace it with a fresh
            # record entirely. A record that is no longer THIS dormant record
            # is not this snapshot's to reclaim; the next daily round decides
            # it against a snapshot taken after whatever just happened.
            current = self._lifecycle.record_for_room(record.room_id)
            if current is not record or not (current.paused or current.dropped_at):
                logger.info(
                    "Reconciliation: room %s changed since the snapshot was "
                    "taken — leaving it for the next round", record.room_id,
                )
                continue
            await self._reclaim_removed_room(
                record.room_id,
                reason="the membership reconciliation found the bot is no "
                       "longer in the room (a removal event was missed)",
                # Pinned to this snapshot's record: the reclaim aborts under
                # the lock if a wake or re-add got there first (round 2) —
                # including an in-place resume, which clears `paused` without
                # replacing the object (round 3, hence require_dormant).
                expected=record,
                require_dormant=True,
            )

    async def shutdown(self) -> None:
        """Stop all processors, save state, disconnect connector.

        Ordering is critical: processors must be stopped FIRST so their final
        live watermarks are flushed back into WatcherState before save_state()
        reads them.  Saving before stop_all() would persist stale watermarks
        and cause duplicate message delivery on the next restart.
        """
        logger.info("SessionManager shutting down")
        if self._watcher_manager is not None:
            # First, before anything stops: the wake arms stay reachable until
            # the connector disconnects, and an idle room's message landing
            # mid-teardown would otherwise recreate a watcher nothing below
            # will stop — absent from stop_all's snapshot, its save rewriting
            # the state file after the final save (§2.5). `drain` disarms AND
            # waits out episodes already in flight (Codex round 5): one
            # already inside `start_watcher_in_room` installs its processor
            # after stop_all's snapshot, so it must finish — or bail at a
            # disarm re-check — before the snapshot is taken.
            await self._watcher_manager.drain()
        # The verbs' half of the same barrier (Codex round 9): resume and
        # reset start watchers off control-socket handlers the ControlServer
        # does not await, so they need their own drain — new transitions
        # refuse, in-flight ones finish before stop_all's snapshot.
        await self._lifecycle.drain_verbs()
        if self._sweep is not None:
            # Before stop_all, so a pass cannot overlap the shutdown's own
            # teardown of the processors it is judging.
            await self._sweep.stop()
        await self._lifecycle.stop_all()
        self._lifecycle.save_state()
        await self._connector.disconnect()
        logger.info("SessionManager shut down")

    # ── Public query API ──────────────────────────────────────────────────────

    def list_watchers(
        self, state_filter: StateFilter = StateFilter.OPERABLE
    ) -> list[dict]:
        return self._lifecycle.list_watchers(state_filter)

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._lifecycle.get_watcher_state(name)

    def get_processor(self, name: str):
        """Return the running MessageProcessor for a watcher, or None.

        "Is a processor running" is a different question from "what state is
        this watcher's record in", and `list` answers only the second (§2.8).
        The old row answered both — `active` from the processor, `paused` from
        the record — which is why callers could reach the first one through
        `list`. They cannot now, and should not: under rule-derived watchers a
        row exists for rooms with no processor by design.
        """
        return self._lifecycle.get_processor(name)

    def get_all_watcher_names(self) -> list[str]:
        """Every persisted watcher name for this connector — records are the
        only watcher identity left (§2.8)."""
        return sorted(self._lifecycle.states().keys())

    async def pause_watcher(self, name: str) -> None:
        await self._lifecycle.pause_watcher(name)

    async def resume_watcher(self, name: str) -> None:
        await self._lifecycle.resume_watcher(name)

    async def reset_watcher(self, name: str) -> None:
        await self._lifecycle.reset_watcher(name)

    async def expire_watcher(self, name: str) -> None:
        """The §2.8 `expire` verb: operator-initiated reclamation, now.

        Runs the same forced-reclamation path a membership removal runs —
        pause overridden with an audit line, scheduled jobs cancelled — but
        raises where the event handlers swallow: an operator watching the
        command must see the failure the connector must not. Only a
        rule-derived record is expirable; a static watcher's owner is
        config.yaml, and there is nothing durable to reclaim for it.
        """
        state = self._lifecycle.get_watcher_state(name)
        if state is None or not state.config or not state.room_id:
            raise RuntimeError(
                f"No expirable record for watcher '{name}' — expire acts on a "
                f"rule-derived record, and this name has none."
            )
        # The destructive verbs join the shutdown barrier (internal review of
        # the barrier close): expire was outside flag+counter, protected only
        # by the ControlServer's stop ordering — incidental and
        # Python-version-dependent. reclaim_room itself stays unbarriered on
        # purpose: its other two callers are the live removal event (entry-
        # gated on disarm) and the reconciliation (rides the sweep task,
        # which shutdown cancels and awaits).
        self._lifecycle._enter_verb("expire", name)
        try:
            reclaimed = await self._lifecycle.reclaim_room(
                state.room_id, reason=f"operator 'expire' on watcher '{name}'",
                # The identity pin, without require_dormant (Codex round 3):
                # the operator selected THIS record — following a replacement
                # would delete a newly created watcher and contradict the
                # error below — but expire acts on active and dormant records
                # alike.
                expected=state,
            )
        finally:
            self._lifecycle._exit_verb()
        if reclaimed is None:
            raise RuntimeError(
                f"Watcher '{name}' was not reclaimed — its record changed "
                f"while the expire ran. Re-check 'list' and retry."
            )
        if self._cancel_jobs is not None:
            try:
                self._cancel_jobs(reclaimed)
            except Exception:
                logger.exception(
                    "Could not cancel scheduled jobs for expired watcher "
                    "'%s' — they will fail audibly when they fire", reclaimed,
                )

    async def inject_message(self, watcher_name: str, text: str) -> bool:
        """Inject a synthetic OWNER-role message directly into a watcher's queue.

        Bypasses the connector layer entirely, avoiding the self-message filter
        that drops messages sent by the bot's own username.  The injected message
        is treated as if it came from a trusted owner, so it is processed without
        permission approval prompts.

        Returns True if the message was accepted into the queue, False otherwise
        (e.g. watcher not running, queue full, or watcher not found).
        """
        processor = self._lifecycle.get_processor(watcher_name)
        if processor is None and self._watcher_manager is not None:
            # The wake, from the inside (§2.5): an idle room's record is real
            # and its session is kept, so a scheduled job due in it recreates
            # the watcher through the same get_or_create a message would —
            # the sweep's expiry exemption for job-bearing rooms rests on
            # exactly this ("idling one is harmless — the job wakes it"), and
            # without it that sentence was an assumption with no backing.
            # Paused answers None, so pause still outranks a schedule (§4.4).
            record = self._lifecycle.get_watcher_state(watcher_name)
            if record is not None and record.room_id:
                kind = _room_kind_or_channel(record)
                processor = await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=record.room_id,
                        kind=kind,
                        name=record.room_name,
                        participants=tuple(record.participants),
                    ),
                )
        if processor is None:
            logger.warning(
                "inject_message: no active processor for watcher %r — "
                "watcher may be paused, stopped, or not configured",
                watcher_name,
            )
            return False

        # Build a minimal Room from persisted state (room_id + room_type)
        state = self._lifecycle.get_watcher_state(watcher_name)
        if state is None:
            logger.warning(
                "inject_message: no persisted state for watcher %r — "
                "room_id will be empty, which may cause the agent response to "
                "be posted to the wrong room or dropped. "
                "Ensure the watcher has been active at least once so its state is persisted.",
                watcher_name,
            )
        room_id = state.room_id if state else ""
        room_name = (state.room_name if state and state.room_name
                     else watcher_name)
        room_type = state.room_type if state else "channel"

        msg = IncomingMessage(
            id=f"sched-{secrets.token_hex(8)}",
            # Epoch milliseconds (as a string), matching the format RC's own
            # messages carry — RocketChatConnector.format_prompt_prefix() feeds
            # this straight into ts_ms_to_iso_local(), which only parses epoch-ms.
            # A plain ISO string here silently drops ts:/day: from the header,
            # which is exactly the "scheduled stock report" scenario in #53.
            timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
            room=Room(id=room_id, name=room_name, type=room_type),
            sender=User(id="scheduler", username="scheduler", display_name="Scheduler"),
            role=UserRole.OWNER,
            text=text,
            attachments=[],
            warnings=[],
            thread_id=None,
            raw={},
        )
        accepted = await processor.enqueue(msg)
        if not accepted:
            logger.warning(
                "inject_message: message for watcher %r was dropped (queue full or processor stopped)",
                watcher_name,
            )
        return accepted

    async def notify_watcher_room(self, watcher_name: str, text: str) -> bool:
        """Send a notification directly to the watcher's room via the connector.

        Bypasses the watcher queue entirely — used for system notifications
        (e.g. scheduler injection failures) that should reach the room even when
        the watcher is paused or its queue is full.

        Returns True if sent successfully, False on error or missing state.
        """
        from ..agents.response import AgentResponse  # local import avoids circular dependency

        state = self._lifecycle.get_watcher_state(watcher_name)
        if state is None or not state.room_id:
            logger.warning(
                "notify_watcher_room: no room_id for watcher %r — cannot send notification",
                watcher_name,
            )
            return False
        try:
            await self._connector.send_text(state.room_id, AgentResponse(text=text))
            return True
        except Exception as e:
            logger.warning(
                "notify_watcher_room: failed to send notification to watcher %r room: %s",
                watcher_name,
                e,
            )
            return False

    # ── Control command dispatch (called by GatewayService) ───────────────────

    async def dispatch_command(self, request: dict) -> dict:
        cmd = request.get("cmd")

        if cmd == "list":
            # An unparseable filter is an error, not a silent fallback to the
            # default: the caller asked a specific question and cannot tell from
            # the rows that it was answered with a different one.
            try:
                state_filter = parse_state_filter(request.get("states"))
            except (ValueError, TypeError) as e:
                # TypeError too: `parse_state_filter` iterates what it is given,
                # and a hand-written socket client can send `"states": 5`. The
                # CLI always sends a list, so this arm is unreachable from it —
                # but escaping as a TypeError turns a bad request into a
                # per-connector "failed to list watchers", which reads as the
                # daemon being broken rather than the request being wrong.
                return {"ok": False, "error": f"invalid 'states' filter: {e}"}
            return {"ok": True, "data": self.list_watchers(state_filter)}

        elif cmd == "pause":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'pause' command"}
            try:
                await self.pause_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "resume":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'resume' command"}
            try:
                await self.resume_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "reset":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'reset' command"}
            try:
                await self.reset_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "expire":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'expire' command"}
            try:
                await self.expire_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        else:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
