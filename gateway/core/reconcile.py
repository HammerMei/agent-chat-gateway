"""Reconcile persisted watcher records against the current rules (design §2.4).

A record freezes the rule it was created from. Between reconciliations that
freeze is authoritative — a rule edit does not reach a running watcher — and
at each reconciliation (boot, and `config reload`) every record is re-matched
against the current ordered rule list:

* the same rule still wins and its snapshot is unchanged → **keep**;
* a rule wins whose name or content differs from what the record froze →
  **rematerialize**: the frozen field group is rewritten from the winning
  rule, the session and the lifecycle clocks are left alone;
* no rule wins → **expire**: the record is reclaimed, the session id logged.

Ownership drift and content drift are one check here, not two: re-running
first-match over the current list catches a rule inserted *above* mine (no
content changed anywhere) and a rule whose body changed alike; the snapshot
digest only decides whether a same-named winner is a change at all.

Pure: this module reads records and rules and returns a plan. Applying it —
rewriting fields, reclaiming records, saving — is the session manager's, so
`config reload --dry-run` can render the same plan without acting on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from .state import WatcherState, connector_name_of, room_kind_or_channel, state_files
from .watcher_manager import (
    RoomRef,
    first_matching_rule,
    materialize,
    rule_bound_fields,
    rule_snapshot,
    snapshot_digest,
)
from .watcher_rule import RoomKind, WatcherRule

Action = Literal["keep", "rematerialize", "expire"]
ExpireReason = Literal["no-rule-matches", "connector-removed"]


@dataclass(frozen=True)
class RecordAction:
    """One record's fate under the current rules."""

    room_id: str
    watcher_name: str
    agent: str
    session_id: str
    action: Action
    from_rule: str = ""
    to_rule: str = ""
    reason: str = ""


@dataclass
class ReconcilePlan:
    """What one connector's records should become under the current rules.

    `connector`, and each action's `agent` and `session_id`, are not read by
    boot — they are the fields a rendered plan (`config reload --dry-run`,
    #144) shows an operator, carried so the renderer does not re-derive them.
    """

    connector: str
    actions: list[RecordAction] = field(default_factory=list)

    def of(self, action: Action) -> list[RecordAction]:
        """The actions of one kind."""
        return [a for a in self.actions if a.action == action]

    @property
    def changes(self) -> list[RecordAction]:
        """Every action that is not a keep — what boot applies and logs."""
        return [a for a in self.actions if a.action != "keep"]

    def summary(self) -> str:
        """One line of counts, for the log."""
        kept = len(self.of("keep"))
        rematerialized = len(self.of("rematerialize"))
        expired = len(self.of("expire"))
        return (f"{kept} kept, {rematerialized} re-materialized, "
                f"{expired} expired")


def room_ref_of(record: WatcherState) -> RoomRef:
    """The room a record describes, shaped the way the rule matcher wants it."""
    return RoomRef(
        id=record.room_id,
        kind=room_kind_or_channel(record),
        name=record.room_name,
        participants=tuple(record.participants),
    )


def rematerialized_fields(record: WatcherState, rule: WatcherRule) -> dict:
    """The frozen fields a re-materialization rewrites, from the winning rule.

    `materialize` and `rule_bound_fields` are the creation path's own, so the
    two writers cannot drift. `room_kind`, `participants` and `created_at` are
    frozen too but describe the room and the birth, not the rule, and stay;
    session-scoped fields and lifecycle clocks are never touched — same room,
    same session, same idle clock. The connector is the record's own (the
    rules were filtered to it), the agent the rule's.
    """
    wc = materialize(rule, room_ref_of(record))
    # The rule's connector IS this manager's connector (the rules were filtered
    # to it), and it wins over the record's column: a state file copied to a
    # renamed connector still carries the old name, and `config_from_record`
    # prefers the column — so a stale one would key prompt and attachment
    # state under a connector that no longer exists.
    return rule_bound_fields(
        wc, rule,
        connector_name=rule.connector,
        agent_name=rule.agent,
    )


NAMELESS = "no room name recorded — cannot re-match, left as it is"
UNKNOWN_KIND = "room kind {kind!r} is not one this build knows — cannot re-match, left as it is"
_KNOWN_KINDS = frozenset(k.value for k in RoomKind)


def orphaned_state_files(configured: Iterable[str]) -> list[tuple[Path, str]]:
    """State files on disk whose connector `config.yaml` no longer names.

    Enumerates files, as `config validate`'s orphan check does, so a renamed
    connector is found by its old file and not by config. Each is
    `(path, connector name)`; the caller decides what to do with the records.
    """
    known = set(configured)
    return [(path, connector_name_of(path)) for path in state_files()
            if connector_name_of(path) not in known]


def reconcile_records(
    records: Iterable[WatcherState],
    rules: list[WatcherRule],
    *,
    connector: str,
) -> ReconcilePlan:
    """Plan what the current rules say about each rule-derived record.

    `rules` is the connector's ordered list (a rule names an agent that exists
    — config loading refuses one that does not — so no agent check is needed
    here). Static-era records (neither `rule_name` nor `config`) are not this
    module's — boot prunes them before reconciling — and are skipped. A record
    that cannot be re-matched honestly — a named room with no name recorded
    (the matcher deliberately does not fall back to the opaque id), or a room
    kind this build does not know — is kept as it is, with the reason:
    "nothing matches" is destructive here.
    """
    plan = ReconcilePlan(connector=connector)
    for record in records:
        if not (record.rule_name or record.config):
            continue
        common = dict(
            room_id=record.room_id,
            watcher_name=record.watcher_name,
            agent=record.agent,
            session_id=record.session_id,
            from_rule=record.rule_name,
        )
        # A kind this build does not know is degraded to CHANNEL everywhere
        # else, which is fine for a runtime fallback and wrong for a match
        # that can expire the record: a DM whose kind was garbled must not be
        # judged as a channel. Kept, with the reason, like a nameless room.
        if record.room_kind and record.room_kind not in _KNOWN_KINDS:
            plan.actions.append(RecordAction(
                action="keep", reason=UNKNOWN_KIND.format(kind=record.room_kind),
                to_rule=record.rule_name, **common))
            continue
        room = room_ref_of(record)
        if room.kind not in (RoomKind.DM, RoomKind.GROUP_DM) and not room.name:
            plan.actions.append(RecordAction(
                action="keep", reason=NAMELESS, to_rule=record.rule_name, **common))
            continue
        winner = first_matching_rule(rules, connector, room)
        if winner is None:
            plan.actions.append(RecordAction(
                action="expire", reason="no-rule-matches", **common))
            continue
        # A snapshot that is not a dict cannot be compared — a damaged record
        # reads as "changed" and is rewritten, never a crash at boot.
        frozen = record.rule if isinstance(record.rule, dict) else None
        unchanged = (
            record.rule_name == winner.name
            and frozen is not None
            and snapshot_digest(frozen) == snapshot_digest(rule_snapshot(winner))
        )
        plan.actions.append(RecordAction(
            action="keep" if unchanged else "rematerialize",
            to_rule=winner.name, **common))
    return plan
