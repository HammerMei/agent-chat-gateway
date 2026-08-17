"""Identity, labels and materialization for rule-derived watchers (design §2.3, §2.4).

A watcher is keyed by `(connector, room_id)` and nothing else. Three separate things are
derived from a room, and conflating any two of them has already been the source of real
defects, so they are named here once:

* **the key** — `(connector, room_id)`. Sticky: once a watcher exists for a key it stays
  bound to it until it expires, and editing or deleting the rule that created it neither
  rebinds nor destroys it (§2.4).
* **the label** — `<connector>-<room label>`, cosmetic, for display and CLI. Free to
  change, free to be ugly. It is deliberately *not* a path component and *not* a lookup
  key; filesystem paths key on a digest (`gateway/core/paths.py`) precisely so the label
  can stay cosmetic.
* **the room description** — a human-meaningful answer to "where does this watcher live",
  written into the materialized config's `room` field and re-supplied to the agent in its
  durable identity header on every turn.

Label and description coincide for a channel and **diverge for a group DM**: label
`gdm-a3f9c1b2`, description `@alice, @bob`. Anything that treats `room` as a lookup key
breaks on that divergence, which is why room resolution goes by `room_id` (§2.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from .adapter_utils import ts_gt as _ts_gt
from .config import HistoryHandoffConfig, WatcherConfig
from .connector import Room
from .pending_route import STARTING_UP_NOTICE
from .room_pattern import RoomPattern
from .state import CONFIG_SCHEMA_VERSION, WatcherState, carried_fields
from .watcher_rule import RoomKind, RuleMatch, WatcherRule

if TYPE_CHECKING:
    from .connector import Connector
    from .message_processor import MessageProcessor
    from .watcher_lifecycle import WatcherLifecycle

logger = logging.getLogger("agent-chat-gateway.core.watcher_manager")

WatcherKey = tuple[str, str]  # (connector, room_id)

# How many hex characters of the room-id digest a group DM's label carries. Eight is the
# design's figure (§2.3): short enough to type from a `list` row, and collisions are
# cosmetic — two identical-looking rows and nothing worse, since the label is never a key.
_GROUP_DM_LABEL_DIGITS = 8

# Characters a label may carry verbatim. Anything else is percent-encoded (§2.3): the old
# sanitizer collapsed them to `-` and raised on an empty result, which made two different
# rooms label identically and could refuse to name a room at all.
_LABEL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

# A display bound, not a correctness one — paths key on a digest, so nothing breaks if a
# label is long; a table column that is several hundred characters wide does. Over the cap
# the label is truncated and given a short room-id hash, so two long names that share a
# prefix stay distinguishable.
_LABEL_MAX = 48


@dataclass(frozen=True)
class RoomRef:
    """Everything creating a watcher needs to know about a room, resolved once.

    A struct rather than a room id plus a name, because creation needs the *kind* and,
    for a group DM, the *participants*: the kind selects the label form and decides
    whether `require_mention` applies, and for a group DM the participants are the only
    description of the room that exists (§2.8).

    `name` is the platform's own name and is empty for both DM kinds. `participants` is
    populated for the DM kinds and empty otherwise.
    """

    id: str
    kind: RoomKind
    name: str = ""
    participants: tuple[str, ...] = ()


def room_label(room: RoomRef) -> str:
    """The room half of a watcher's display label, per kind (§2.3).

    | kind | label | stable? |
    |---|---|---|
    | channel / private group | the channel name | until renamed |
    | 1:1 DM | `dm-<counterpart>` | until the counterpart is renamed (§2.3) |
    | group DM | `gdm-<8 hex of the room-id digest>` | yes, by construction |

    **A group DM's members are deliberately not in its label.** The tempting alternative
    is Mattermost's `channel_display_name`, which *is* the member list — but it moves
    whenever membership does, it includes the bot's own name, its ordering is
    undocumented, and Rocket.Chat has no equivalent, so the two platforms would label one
    kind of room by different rules. A digest of the room id is identical on both, stable
    for the room's life, and short enough to type; the members belong in a `list` column,
    which is information about the room rather than its name.
    """
    if room.kind is RoomKind.DM:
        # One counterpart by definition. Falling back to the digest rather than raising:
        # a label is cosmetic, and refusing to name a room would be a worse failure than
        # naming it dully.
        counterpart = room.participants[0] if room.participants else _digest(room.id)
        return _encode(f"dm-{counterpart}", room.id)
    if room.kind is RoomKind.GROUP_DM:
        return f"gdm-{_digest(room.id)}"
    return _encode(room.name, room.id) if room.name else _digest(room.id)


def watcher_label(connector: str, room: RoomRef) -> str:
    """`<connector>-<room label>` — the display and CLI handle (§2.3).

    Unique by construction, because connector names are validated unique at config load
    and one connector serves one namespace of room names. On Mattermost a channel name is
    unique only within a team, so this holds *because* one connector serves one team
    (§6.3): the room name is really `(team, channel)` even though only the channel part
    appears here.
    """
    return f"{connector}-{room_label(room)}"


def room_description(room: RoomRef) -> str:
    """What `WatcherConfig.room` holds for a materialized watcher — never a pattern.

    This is the field the durable identity header renders as `- **Room:** …`, appended to
    the system prompt on every turn so it survives compaction. A rule-shaped value would
    permanently tell an agent its room is `eng-*`.

    For a group DM the participants *are* the description; there is nothing else to say.
    """
    if room.kind is RoomKind.GROUP_DM:
        return ", ".join(room.participants) if room.participants else room_label(room)
    if room.kind is RoomKind.DM:
        return room.participants[0] if room.participants else room_label(room)
    return room.name or room.id


def materialize(rule: WatcherRule, room: RoomRef) -> WatcherConfig:
    """Turn a rule plus a resolved room into the concrete config a watcher runs on (§2.4).

    Every field is carried across unchanged except the two that a rule cannot supply:

    * `name` → the derived label. A rule's `name` is the *rule's* identity, reused across
      every room it matches, so it cannot be a watcher's name.
    * `room` → the concrete description. Never the pattern.

    The rule's own lifecycle fields (`session_idle_days`, `session_expire_days`) are
    deliberately *not* copied here: they belong to the rule, and `WatcherConfig` has no
    home for them. The state record keeps the resolved rule alongside the materialized
    config for exactly this reason (§2.4, "two records, not one") — recreation reads the
    config, drift detection reads the rule.
    """
    return WatcherConfig(
        name=watcher_label(rule.connector, room),
        connector=rule.connector,
        room=room_description(room),
        agent=rule.agent,
        context_inject_files=list(rule.context_inject_files),
        online_notification=rule.online_notification,
        offline_notification=rule.offline_notification,
        history_handoff=replace(rule.history_handoff),
    )


def rule_snapshot(rule: WatcherRule) -> dict:
    """The rule as resolved at creation, in a JSON-safe form — the drift baseline (§2.4).

    **Derived from the dataclass, not from a hand-written field list.** The first version
    listed every field, and review found `history_handoff.max_fetch_count` missing: two
    rules differing only in that cap produced identical snapshots, so drift could not
    report it. A list that has to be updated whenever a field is added is the same shape
    as the hand-maintained type table `state.py` deliberately derives instead.

    `dataclasses.asdict` still cannot do it: a rule's patterns are `RoomPattern` objects (a
    `__slots__` class, compiled at load so an invalid pattern cannot reach the delivery
    path), which `asdict` leaves as objects — the record would fail to serialize on its
    first save. Patterns become the raw strings they were compiled from, which is also the
    only form an operator can compare against their own `config.yaml`.

    **Stored resolved, after `inherits:` has been applied.** Template inheritance is
    flattened at parse time, so the resolved form is what the parser naturally produces —
    and storing it means an edit to a watcher *template* registers as drift for free.
    Storing the raw YAML entry instead would let template changes escape detection.

    The materialized config cannot serve as this baseline: its `name` and `room` are
    overwritten by construction, so diffing it against a rule would report those two
    fields as changed every time.
    """
    return _jsonable(rule)


def _jsonable(value):
    """Recursively convert a rule into JSON-safe data, walking dataclass fields.

    Only one type needs special handling — `RoomPattern`, which is not a dataclass — so
    everything else is reached generically and a field added to any rule dataclass appears
    in the snapshot without this function changing.
    """
    if isinstance(value, RoomPattern):
        return value.raw
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def snapshot_digest(snapshot: dict) -> str:
    """A stable hash of a rule snapshot, for "has anything changed?" without a diff.

    Sorted keys and no whitespace, so the digest depends on content rather than on how
    the dict happened to be built. Cheap equality only — showing an operator *what*
    changed needs the full snapshot, which is why both are stored (§2.4).

    Not a substitute for the ownership check: under first-match precedence a rule inserted
    *above* mine starts winning for my room without any rule's content changing, so that
    is detected by re-running the match against the current ordered list, never by this.
    """
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def first_matching_rule(
    rules: list[WatcherRule], connector: str, room: RoomRef
) -> WatcherRule | None:
    """The rule that owns this room, or None if no rule claims it (§2.2).

    **First match in config order wins, and a decline halts the search.** `except_for`
    produces `DECLINED` rather than falling through, so a room an earlier rule excludes
    does not reach a later one — a deny rule (`include: [X]`, `except_for: [X]`) shadows
    later rules for X completely, which is its purpose. Returning the first *claim* while
    letting a decline fall through would quietly invert that.

    Rules for other connectors are skipped rather than matched-and-discarded: a rule's
    `connector` is part of what it claims, not a filter applied afterwards.

    **A named room with no name matches nothing**, rather than falling back to its id.
    An earlier version substituted `room.id`, so a room whose opaque id happened to look
    like a configured pattern could be claimed — or excluded — on the strength of a string
    the operator never wrote and cannot see. `room_label` still falls back to a digest for
    such a room, because a label is cosmetic; a *match* is not.
    """
    if room.kind not in (RoomKind.DM, RoomKind.GROUP_DM) and not room.name:
        return None

    for rule in rules:
        if rule.connector != connector:
            continue
        # The name is passed as-is, including the empty string a DM carries: `match()`
        # short-circuits on kind for both DM kinds before consulting any pattern, so a
        # pattern cannot claim a DM and a DM opt-in cannot claim a channel. An earlier
        # version passed the *label* for group DMs, to avoid a pattern matching a digest —
        # a branch whose premise the matcher makes impossible, found by injecting the
        # fault and watching nothing fail.
        verdict = rule.match(room.name, room.kind)
        if verdict is RuleMatch.CLAIMED:
            return rule
        if verdict is RuleMatch.DECLINED:
            return None
    return None


def _encode(raw: str, room_id: str) -> str:
    """A label component made safe to print and to type, per §2.3.

    Percent-encoding rather than the old collapse-to-`-`: that mapped every unsafe
    character onto one, so two different rooms could produce identical labels, and it
    raised when nothing survived. This never raises and is reversible by eye.

    Platforms differ in what they allow — Rocket.Chat and Mattermost emit slugs, but the
    voice connector takes its room from a URL path, so `/ask/a/b` yields the room `a/b`,
    and a raw label would carry a character the static config path rejects in a watcher
    name. Encoding here keeps one handle syntax across both shapes.

    Over the length cap the label is truncated and a short room-id hash appended, so two
    long names sharing a prefix remain distinguishable.
    """
    encoded = "".join(
        c if c in _LABEL_SAFE else "".join(f"%{b:02X}" for b in c.encode())
        for c in raw
    )
    if len(encoded) <= _LABEL_MAX:
        return encoded
    keep = _LABEL_MAX - _GROUP_DM_LABEL_DIGITS - 1
    return f"{encoded[:keep]}-{_digest(room_id)}"


def _digest(room_id: str) -> str:
    """Stable short hex for a room id.

    Its own function rather than a call to `gateway/core/paths.py`'s keying: that digest
    is a **filesystem key** and must never change, while this one is cosmetic. Sharing
    them would tie a display decision to a value that names files on disk.
    """
    return hashlib.sha256(room_id.encode()).hexdigest()[:_GROUP_DM_LABEL_DIGITS]


def config_from_record(record: WatcherState) -> WatcherConfig | None:
    """Rebuild the materialized config a record was persisted with (§2.4).

    Recreation reads the record, never the current rule — that is what sticky
    binding means. The record's `config` was written by `_jsonable(materialize(...))`,
    so the shape is ACG's own; still, every read below tolerates absence and wrong
    types by returning None rather than raising, because the caller's correct answer
    to an unreadable record is "decline to recreate and log", not a traceback on the
    routing path. A record with no `config` at all is the static model's (its
    recreation source is `config.yaml`) and returns None for the same reason.
    """
    raw = record.config
    if not isinstance(raw, dict) or not raw:
        return None
    if not isinstance(raw.get("name"), str) or not raw["name"]:
        return None

    def _str(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    hh_raw = raw.get("history_handoff")
    hh_kwargs = {}
    if isinstance(hh_raw, dict):
        for f in fields(HistoryHandoffConfig):
            if f.name in hh_raw and isinstance(hh_raw[f.name], type(f.default)):
                hh_kwargs[f.name] = hh_raw[f.name]
    files = raw.get("context_inject_files")
    return WatcherConfig(
        name=raw["name"],
        connector=_str("connector"),
        room=_str("room"),
        agent=_str("agent"),
        context_inject_files=[p for p in files if isinstance(p, str)]
        if isinstance(files, list) else [],
        online_notification=_str("online_notification") or None,
        offline_notification=_str("offline_notification") or None,
        history_handoff=HistoryHandoffConfig(**hh_kwargs),
    )


class WatcherManager:
    """The runtime half of §2.8: rule-derived creation on the message path.

    One instance per connector, alongside that connector's `WatcherLifecycle` —
    the lifecycle owns the start machinery and the state dicts; this class owns
    the *decision* to create (rule match, sticky binding, single-flight, the
    concurrent-creation cap) and the §5.3 record fields only a creation knows
    (rule snapshot, room kind, participants, creation time).

    Deliberately narrow for now: `get_or_create` is the message path and is the
    only caller that exists. The remaining §2.8 surface (`get` for injection and
    scheduled jobs, the operator verbs, `resolve`) lands with the increments
    that call it — shipping it earlier would be interface with no consumer,
    which this series has been corrected on twice.
    """

    def __init__(
        self,
        connector_name: str,
        connector: "Connector",
        lifecycle: "WatcherLifecycle",
        rules: list[WatcherRule],
        *,
        creation_cap: int = 4,
    ) -> None:
        self._connector_name = connector_name
        self._connector = connector
        self._lifecycle = lifecycle
        self._rules = rules
        # Per-room single-flight (§2.7 step 4). The connectors' own
        # `_rooms_being_routed` sets narrow the window but do not close it: they
        # are released before creation's awaits finish on RC's routing workers,
        # and this lock is what actually covers "existence check + creation" as
        # one critical section. Entries are never removed — the map grows with
        # distinct rooms offered, which is bounded by rooms the account can see;
        # expiry's reclamation sweep is the owner of shrinking it.
        self._locks: dict[str, asyncio.Lock] = {}
        # A soft cap, checked-then-incremented under the per-room lock. Two
        # rooms' creations can race the check and both proceed; the cap bounds
        # pile-ups, it does not ration exactly (§2.7 step 7).
        self._creation_cap = creation_cap
        self._creations_in_flight = 0

    async def get_or_create(
        self,
        connector: str,
        room: RoomRef,
        *,
        history_before_ts: str | None = None,
    ) -> "MessageProcessor | None":
        """A ready watcher for this room, created from the first matching rule
        if the room has never had one (§2.8). None means "no watcher": no rule
        claims the room, its record is paused, or creation is over the cap.

        `history_before_ts` bounds a new session's history handoff strictly
        below the triggering message, so the trigger is not delivered twice.

        **None is a final answer; an exception is a retryable one (§2.2).**
        None means the decision was made and it was "no watcher" — a rule miss
        and a paused record are completed decisions, and re-asking cannot
        change them. A creation or recreation that *raises* is the opposite:
        the decision was never carried out, the message must stay eligible for
        redelivery, and the caller owns the retry. Collapsing the two shapes
        into None made the transaction's abort outcome unreachable, which is
        why this method deliberately does not catch what the start raises.
        The cap refusal answers None too: it already produced a visible
        "starting up" notice, and the next message re-asks.
        """
        if connector != self._connector_name:
            # A wiring error, not a routing outcome — each connector's router
            # closure names its own connector.
            logger.error(
                "get_or_create for connector %r reached the manager for %r",
                connector, self._connector_name,
            )
            return None

        lock = self._locks.setdefault(room.id, asyncio.Lock())
        async with lock:
            record = self._lifecycle.record_for_room(room.id)
            if record is not None:
                resident = self._lifecycle.processor_named(record.watcher_name)
                if resident is not None:
                    return resident
                return await self._recreate(record, room, history_before_ts)
            return await self._create(room, history_before_ts)

    async def _recreate(
        self,
        record: WatcherState,
        room: RoomRef,
        history_before_ts: str | None,
    ) -> "MessageProcessor | None":
        """Sticky binding (§2.4): a room with a record is recreated from its own
        persisted config; the current rules are never consulted."""
        if record.paused:
            # An explicit pause is never overridden by inference (§4.4). The
            # message is deliberately dropped, not deferred.
            logger.debug(
                "Room %s has a paused record ('%s') — not recreating",
                record.room_id, record.watcher_name,
            )
            return None
        wc = config_from_record(record)
        if wc is None:
            # A static-model record (no frozen config). Its recreation source is
            # config.yaml and its owner is sync_watchers' retry-at-every-start
            # rule — recreating it from here would invent a second owner.
            logger.debug(
                "Room %s's record ('%s') carries no materialized config — "
                "leaving it to the static path",
                record.room_id, record.watcher_name,
            )
            return None
        platform_room = Room(
            id=record.room_id,
            # The record's own kind, not the offered room's: the record is what
            # this watcher was created against (§2.4), and a re-classification
            # that disagreed would silently change whether the mention gate
            # applies to a room that already has a watcher.
            name=room_description(room),
            type=record.room_kind or room.kind.value,
        )
        # Everything a start does not rebuild, carried out of the record being
        # recreated — derived from the field classification in `state.py`, not
        # listed here, so a new §5.3 field survives recreation without this line
        # changing. The clocks move with it: the room is resident again, so it
        # is no longer dropped, and this is activity.
        carried = carried_fields(record)
        carried["last_activity_at"] = _now_iso()
        carried["dropped_at"] = ""
        # The window this recreation owes the room. Read before the start, which
        # restores it into the connector and then advances it as the replayed
        # and live messages commit.
        boundary = record.last_processed_ts
        # A raise propagates: recreation that failed is an abort, not a decision,
        # and the caller owns the retry (§2.2). The per-room lock releases on the
        # way out, so the retry can re-enter.
        await self._lifecycle.start_watcher_in_room(
            wc, record, platform_room,
            # The record's own watermark bounds the handoff when the backend has
            # expired the session and a fresh one has to be minted: without it the
            # unbounded fetch pulls in the very interval the replay below is about
            # to deliver, and the agent sees it twice.
            #
            # The **lower** of the two bounds wins, and that is not the same as
            # "the trigger's, if there is one". `before_ts` is an exclusive upper
            # bound, so a *lower* value fetches less; the trigger is by
            # construction a message above the watermark, so preferring it would
            # pick the looser bound and re-admit the whole interval — the exact
            # double delivery this argument exists to prevent, on the common path.
            history_before_ts=_earlier(history_before_ts, boundary),
            provenance=carried,
        )
        self._lifecycle.save_state()

        # Everything above the record's watermark, replayed through the normal
        # pipeline. This is what makes an abort recoverable for a room that has
        # a record (§2.2): the routing episode that parked, or the buffer that
        # overflowed, left its frames below this boundary, and nothing else
        # would ever return to them — the reconnect replay iterates *tracked*
        # rooms and a parked room is untracked, and the next live message's
        # commit would seal the interval by advancing the watermark past it.
        #
        # Best-effort: the room is up either way, and a replay that fails must
        # not undo a successful recreation. Bounded by the record's own mark,
        # so the room's replay boundary is not spent by a window it did not set.
        if boundary:
            try:
                await self._connector.replay_room_since(record.room_id, after_ts=boundary)
            except Exception as e:
                logger.warning(
                    "Watcher '%s' is up, but replaying room %s from %s failed — "
                    "messages from before it may stay undelivered: %s",
                    wc.name, record.room_id, boundary, e,
                )
        return self._lifecycle.processor_named(wc.name)

    async def _create(
        self,
        room: RoomRef,
        history_before_ts: str | None,
    ) -> "MessageProcessor | None":
        """First-ever watcher for this room: match, cap, materialize, start,
        and freeze the §5.3 record fields only this moment knows."""
        rule = first_matching_rule(self._rules, self._connector_name, room)
        if rule is None:
            return None

        if self._creations_in_flight >= self._creation_cap:
            # §2.7 step 7: queue depth bounds messages per room; this bounds
            # rooms being created at once. The honest answer is a visible
            # "starting up", not a silent drop — the sender watched their
            # message arrive.
            logger.warning(
                "Creation cap (%d) reached — deferring watcher creation for "
                "room %s", self._creation_cap, room.id,
            )
            try:
                # send_text, not send_to_room: the latter resolves its argument
                # as a room *name*, and all this layer holds is the opaque id.
                from ..agents.response import AgentResponse

                await self._connector.send_text(
                    room.id, AgentResponse(text=STARTING_UP_NOTICE))
            except Exception:
                logger.debug("Could not post the starting-up notice", exc_info=True)
            return None

        wc = materialize(rule, room)
        platform_room = Room(
            id=room.id,
            name=room_description(room),
            type=room.kind.value,
        )
        # The §5.3 fields only this moment knows (§2.4), handed to the start so
        # they are part of the record from its first instant. Written here rather
        # than onto the record afterwards: an enrichment step leaves a window in
        # which a concurrent creation's save persists this record without its
        # rule, and a crash in that window leaves an orphan for the next boot to
        # prune.
        now = _now_iso()
        provenance = {
            "room_kind": room.kind.value,
            "participants": list(room.participants),
            "connector": self._connector_name,
            "agent": self._lifecycle.resolve_agent_name(wc.agent),
            "created_at": now,
            "last_activity_at": now,
            "dropped_at": "",
            "config": _jsonable(wc),
            "rule_name": rule.name,
            "rule": rule_snapshot(rule),
            "config_schema_version": CONFIG_SCHEMA_VERSION,
        }
        self._creations_in_flight += 1
        try:
            # A raise propagates (§2.2): a creation that failed is an abort, and
            # catching it here would hand the caller the same None a rule miss
            # produces — a final answer for a non-final condition. The cap slot
            # and the per-room lock both release on the way out.
            await self._lifecycle.start_watcher_in_room(
                wc, None, platform_room,
                history_before_ts=history_before_ts, provenance=provenance,
            )
        finally:
            self._creations_in_flight -= 1

        self._lifecycle.save_state()
        logger.info(
            "Created watcher '%s' for room %s from rule '%s'",
            wc.name, room.id, rule.name,
        )
        return self._lifecycle.processor_named(wc.name)


def _earlier(a: str | None, b: str | None) -> str | None:
    """The tighter of two exclusive upper bounds, or whichever one exists.

    A function rather than an inline comparison because the direction is easy
    to get backwards and was: an upper bound is tighter when it is *lower*, and
    the first version reached for the trigger's bound as "the tighter of the
    two" when the trigger is by construction above the watermark.
    """
    if not a:
        return b or None
    if not b:
        return a
    return a if _ts_gt(b, a) else b


def _now_iso() -> str:
    """Local-time ISO seconds — the same shape every other timestamp in the
    state file carries."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
