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

import hashlib
from dataclasses import dataclass, replace

from .config import WatcherConfig
from .watcher_rule import RoomKind, RuleMatch, WatcherRule

WatcherKey = tuple[str, str]  # (connector, room_id)

# How many hex characters of the room-id digest a group DM's label carries. Eight is the
# design's figure (§2.3): short enough to type from a `list` row, and collisions are
# cosmetic — two identical-looking rows and nothing worse, since the label is never a key.
_GROUP_DM_LABEL_DIGITS = 8


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
    | 1:1 DM | `dm-<counterpart>` | yes — a username is stable |
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
        return f"dm-{room.participants[0]}" if room.participants else f"dm-{_digest(room.id)}"
    if room.kind is RoomKind.GROUP_DM:
        return f"gdm-{_digest(room.id)}"
    return room.name or _digest(room.id)


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

    Not `dataclasses.asdict`: a rule's patterns are `RoomPattern` objects (a `__slots__`
    class, compiled at load so an invalid pattern cannot reach the delivery path), and
    `asdict` leaves them as objects, so the record would fail to serialize on the first
    save. Patterns are stored as the raw strings they were compiled from, which is also
    the only form an operator can compare against their own `config.yaml`.

    **Stored resolved, after `inherits:` has been applied.** Template inheritance is
    flattened at parse time, so the resolved form is what the parser naturally produces —
    and storing it means an edit to a watcher *template* registers as drift for free.
    Storing the raw YAML entry instead would let template changes escape detection.

    The materialized config cannot serve as this baseline: its `name` and `room` are
    overwritten by construction, so diffing it against a rule would report those two
    fields as changed every time.
    """
    matcher = rule.rooms
    return {
        "name": rule.name,
        "connector": rule.connector,
        "agent": rule.agent,
        "rooms": {
            "include": [p.raw for p in matcher.include],
            "except_for": [p.raw for p in matcher.except_for],
            "direct": matcher.direct,
            "group_direct": matcher.group_direct,
        },
        "session_idle_days": rule.session_idle_days,
        "session_expire_days": rule.session_expire_days,
        "context_inject_files": list(rule.context_inject_files),
        "online_notification": rule.online_notification,
        "offline_notification": rule.offline_notification,
        "history_handoff": {
            "enabled": rule.history_handoff.enabled,
            "fetch_count": rule.history_handoff.fetch_count,
            "verbatim_tail": rule.history_handoff.verbatim_tail,
        },
    }


def snapshot_digest(snapshot: dict) -> str:
    """A stable hash of a rule snapshot, for "has anything changed?" without a diff.

    Sorted keys and no whitespace, so the digest depends on content rather than on how
    the dict happened to be built. Cheap equality only — showing an operator *what*
    changed needs the full snapshot, which is why both are stored (§2.4).

    Not a substitute for the ownership check: under first-match precedence a rule inserted
    *above* mine starts winning for my room without any rule's content changing, so that
    is detected by re-running the match against the current ordered list, never by this.
    """
    import json

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
    """
    for rule in rules:
        if rule.connector != connector:
            continue
        # The name is passed as-is, including the empty string a DM carries: `match()`
        # short-circuits on kind for both DM kinds before consulting any pattern, so a
        # pattern cannot claim a DM and a DM opt-in cannot claim a channel. An earlier
        # version passed the *label* for group DMs, to avoid a pattern matching a digest —
        # a branch whose premise the matcher makes impossible, found by injecting the
        # fault and watching nothing fail.
        verdict = rule.match(room.name or room.id, room.kind)
        if verdict is RuleMatch.CLAIMED:
            return rule
        if verdict is RuleMatch.DECLINED:
            return None
    return None


def _digest(room_id: str) -> str:
    """Stable short hex for a room id.

    Its own function rather than a call to `gateway/core/paths.py`'s keying: that digest
    is a **filesystem key** and must never change, while this one is cosmetic. Sharing
    them would tie a display decision to a value that names files on disk.
    """
    return hashlib.sha256(room_id.encode()).hexdigest()[:_GROUP_DM_LABEL_DIGITS]
