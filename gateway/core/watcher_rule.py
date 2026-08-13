"""Watcher rules: how to build a watcher, rather than which room to watch.

A `watchers:` entry under the model in `docs/design/dynamic-watcher-design.md`
§2.1 names no room. It describes the parameters a watcher should be built with
when a message arrives from a room the rule claims. Rooms are never enumerated
in config.

Two types, not one, and the split is the design's (§2.4 has a watcher persist
"a materialized config plus the originating rule"; §5.3 stores `config` and
`rule` as separate state fields):

* **`WatcherRule`** — this module. Parsed from config, names no room.
* **`WatcherConfig`** — `gateway.core.config`. Unchanged, and *not* what this
  replaces: it is the materialization, what a rule becomes once bound to one
  concrete room, and what gets persisted so the watcher can be recreated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from gateway.core.config import HistoryHandoffConfig
from gateway.core.room_pattern import RoomPattern

__all__ = [
    "RoomKind",
    "RuleMatch",
    "RoomMatcher",
    "WatcherRule",
]


class RoomKind(Enum):
    """What kind of room a message came from.

    The kind decides which half of a rule applies — name patterns for named
    rooms, the DM opt-in flags for direct messages — and, later, whether
    `require_mention` applies at all (§6.4: it is skipped for 1:1 DMs but must
    not be for group DMs).
    """

    CHANNEL = "channel"
    GROUP = "group"
    DM = "dm"
    GROUP_DM = "group_dm"

    @property
    def is_direct(self) -> bool:
        return self in (RoomKind.DM, RoomKind.GROUP_DM)


class RuleMatch(Enum):
    """The outcome of testing one rule against one room.

    Three outcomes rather than a boolean, because §2.1 makes `exclude` a
    within-rule veto rather than a routing operator: "an excluded room does
    **not** fall through to a later rule — the rule claimed it and then declined
    it". Collapsing `DECLINED` into `NO_MATCH` would let a later rule pick up a
    room an earlier rule explicitly excluded, and two rules could then silently
    contend for it.
    """

    NO_MATCH = "no_match"
    """This rule has nothing to say; try the next rule."""

    CLAIMED = "claimed"
    """This rule owns the room; build a watcher from it."""

    DECLINED = "declined"
    """An include matched and an exclude vetoed. Stop — do not try later rules,
    and do not build a watcher."""


@dataclass(frozen=True)
class RoomMatcher:
    """Which rooms a rule claims.

    `include`/`exclude` are compiled globs over room names; `direct` and
    `group_direct` are opt-ins for the two DM kinds, which have no name to match
    against on either platform (§2.7). All four can appear on one rule: the
    patterns govern named rooms and the flags govern DMs, independently.
    """

    include: tuple[RoomPattern, ...] = ()
    exclude: tuple[RoomPattern, ...] = ()
    direct: bool = False
    group_direct: bool = False

    def match(self, name: str, kind: RoomKind) -> RuleMatch:
        """Test one room against this rule.

        `exclude` is deliberately **not** consulted for DMs. In the boolean form
        there is nothing for a name pattern to match — a 1:1 DM has no room name
        on Rocket.Chat at all — so an exclude list could only ever be a no-op
        there. §2.7 records the object form (`direct: {include: [...],
        exclude: [...]}`) as the additive extension for when per-DM control is
        genuinely needed.
        """
        if kind is RoomKind.DM:
            return RuleMatch.CLAIMED if self.direct else RuleMatch.NO_MATCH
        if kind is RoomKind.GROUP_DM:
            return RuleMatch.CLAIMED if self.group_direct else RuleMatch.NO_MATCH

        if not any(p.matches(name) for p in self.include):
            return RuleMatch.NO_MATCH
        if any(p.matches(name) for p in self.exclude):
            return RuleMatch.DECLINED
        return RuleMatch.CLAIMED

    @property
    def claims_only_direct(self) -> bool:
        """True when this rule reaches DMs and nothing else.

        An empty `include` is a hard load error *unless* this holds — otherwise
        the rule can never match anything, which is a typo rather than an
        intention.
        """
        return not self.include and (self.direct or self.group_direct)


@dataclass
class WatcherRule:
    """A `watchers:` entry: the parameters for watchers this rule creates.

    Deliberately not `frozen=True`, matching `WatcherConfig`: a frozen dataclass
    generates `__hash__` from its fields, and both `context_inject_files` (a
    list) and `HistoryHandoffConfig` (a mutable dataclass) are unhashable — so
    hashing a rule would raise rather than being merely unavailable. `RoomMatcher`
    *is* frozen, because tuples of `RoomPattern` genuinely are hashable.

    `name` is the **rule's** identity, not a watcher's — operator-supplied,
    required and unique, used to report shadowing at load and to attribute
    watchers to the rule that created them (§5.3 persists it as `rule_name`).
    The watcher's own name is derived per room (§2.3), which is what makes
    `name` mandatory here: there is no longer a room to derive it from.

    Order within `watchers:` is load-bearing — first match wins (§2.1) — so
    rules are kept in a list and never keyed by name.
    """

    name: str
    connector: str
    agent: str
    rooms: RoomMatcher
    session_idle_days: int | None = None
    session_expire_days: int | None = None
    context_inject_files: list[str] = field(default_factory=list)
    online_notification: str | None = None
    offline_notification: str | None = None
    history_handoff: HistoryHandoffConfig = field(default_factory=HistoryHandoffConfig)

    def match(self, name: str, kind: RoomKind) -> RuleMatch:
        return self.rooms.match(name, kind)
