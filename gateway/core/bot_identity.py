"""One bot account, one connector — stated once, checked in two places (§4.5).

Under subscribe-all, a connector receives everything its account can see, so two
connectors sharing an account receive identical streams and every room matching rules
on both gets two agents answering. The `(connector, room_id)` watcher key cannot see
it: the records differ in their connector component and each connector writes its own
state file.

**Config cannot decide this.** Mattermost supports token-only auth, where `username` is
empty, and two *different* tokens can authenticate the *same* account — so comparing
config fields misses precisely the case the rule exists to catch, and comparing tokens
misses it too. The enforcement point is therefore runtime, after each connector has
authenticated and can report the identity the *platform* gave it.

The load-time check in `gateway/config.py` is an optimisation on top of this, not a
second rule: it compares declared credentials so an obvious mistake fails at
`config validate` rather than at startup. Both call `find_identity_conflicts()` so the
rule has one statement. Where the two disagree, the runtime one is right.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .room_pattern import is_direct_room_name

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class ConnectorIdentityError(Exception):
    """A connector could not establish who it is authenticated as.

    Fail-closed on purpose: a connector that cannot answer this question cannot be
    checked against the others, and starting it unvalidated is the situation §4.5
    exists to prevent. Raised by a connector's own `bot_identity()`.
    """


class DuplicateBotIdentityError(Exception):
    """Two connectors are the same bot account on the same server."""


def canonical_origin(url: str) -> str:
    """`scheme://host[:port][/path]` — comparable, from a URL a human typed.

    Two connectors pointing at one server routinely spell it differently:
    `https://mm.example.com`, the same with a trailing slash, and
    `https://mm.example.com:443` are one origin, and comparing the raw strings would
    call them three accounts and let the duplicate through. The same lesson as
    canonicalizing a working directory before comparing session identity — a
    comparison is only as good as the normalisation in front of it.

    Normalisation has a second failure direction, and it is the worse one: collapsing
    things that are genuinely different produces a *false* duplicate, and this check
    responds to a duplicate by refusing to start.

    * **The path is kept.** Two deployments can share a host under different prefixes
      (`https://host/rc-one`, `https://host/rc-two`) — the REST and WebSocket clients
      both build their URLs on that prefix, so those are two servers. Dropping it would
      refuse a valid pair.
    * **An IPv6 host keeps its brackets.** Without them `https://[::1]:8443` and
      `https://[::1:8443]` both render as `https://::1:8443`, one address-and-port and
      one address, indistinguishable.
    * **An unparseable port is left alone rather than normalised.** `urlsplit().port`
      raises on a non-numeric or out-of-range port, and this function is reached from
      `acg config validate`, where a traceback replaces the attributed bad-URL finding
      the operator needs. The malformed string becomes its own origin: it compares
      equal only to an identical mistake, which is the harmless answer.

    The default port for the scheme is dropped, so the explicit and implicit spellings
    converge. Query and credentials are discarded: they address a request, not a server.
    """
    parsed = urlsplit(url if "//" in url else f"//{url}", scheme="https")
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if ":" in host:  # IPv6 literal — brackets are what make host and port separable
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # Not a number, or outside 1-65535. Nothing here can repair it, and raising
        # would crash a validation run that exists to report exactly this kind of typo.
        return f"{scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    authority = f"{host}:{port}" if port else host
    return f"{scheme}://{authority}{parsed.path.rstrip('/')}"


@dataclass(frozen=True)
class BotIdentity:
    """Who a connector is authenticated as, as the *platform* reports it.

    `scope` is Mattermost's resolved team id, and is the one exception to "one account,
    one connector": the socket spans every team the account belongs to, so two
    connectors on one account are safe for *channels* provided each discards events for
    other teams. It must be the **resolved** team id, never the configured `team:`
    string — that field accepts a name or an id, so two connectors on one team can spell
    it two ways and compare as different.

    An empty `scope` means the connector has no sub-scope to separate it from another on
    the same account, which is Rocket.Chat's situation: no team concept, so two
    connectors on one account duplicate everything.

    `platform` is part of the key because user ids live in per-platform id spaces. A
    Rocket.Chat and a Mattermost deployment reachable at one origin are two independent
    authentication realms, and an id — or, at config-validation time, a conventional
    username like `bot` — colliding across them is a coincidence, not one account. Two
    connectors of different types can never be the same account, so keeping the platform
    in the key costs nothing and removes a class of false refusals.
    """

    platform: str
    origin: str
    user_id: str
    scope: str = ""


@dataclass(frozen=True)
class ConnectorIdentity:
    """One connector's answer, plus whether it claims the account's DM stream."""

    connector_name: str
    identity: BotIdentity
    owns_dms: bool = False


def dm_owner_connectors(watchers, watcher_rules) -> set[str]:
    """Which connectors claim their account's direct-message stream.

    Takes both watcher shapes because both can reach a DM, and only one of them can
    reach anything at all today: a rule opts in with `direct:`/`group_direct:`, while a
    **static** watcher names the room outright as `@someone` — the `@` convention both
    platforms implement (`rocketchat/rest.py`, `mattermost/rest.py`, where it resolves
    to a direct channel). Rules have no runtime effect until the watcher manager lands,
    so counting only rules would have left this condition guarding the one form that
    cannot yet happen while missing the one that can.

    Takes the two lists rather than a `GatewayConfig` because this module is in
    `gateway.core`, which the config layer imports and must not import back.
    """
    owners = {
        rule.connector
        for rule in watcher_rules
        if rule.rooms.direct or rule.rooms.group_direct
    }
    owners |= {w.connector for w in watchers if is_direct_room_name(w.room)}
    return owners


def find_identity_conflicts(entries: list[ConnectorIdentity]) -> list[str]:
    """Every reason these connectors cannot run together, as operator-facing lines.

    Returns all conflicts rather than the first: a config with three colliding
    connectors should not need three restarts to learn that.

    The rule, applied per `(origin, user_id)` group:

    * a group of one is always fine;
    * a group of more than one is a conflict **unless** every member has a distinct,
      non-empty `scope` — the Mattermost different-teams exception;
    * an excepted group may still contain **at most one** DM owner. A DM has no team, so
      the team gate cannot separate it and the platform delivers it to every socket the
      account has open.
    """
    conflicts: list[str] = []
    groups: dict[tuple[str, str, str], list[ConnectorIdentity]] = {}
    for e in entries:
        key = (e.identity.platform, e.identity.origin, e.identity.user_id)
        groups.setdefault(key, []).append(e)

    for (_platform, origin, user_id), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        names = ", ".join(sorted(f"'{e.connector_name}'" for e in group))
        scopes = [e.identity.scope for e in group]
        if any(not s for s in scopes) or len(set(scopes)) != len(scopes):
            conflicts.append(
                f"Connectors {names} authenticate as the same bot account "
                f"({user_id} on {origin}). Two connectors sharing an account receive "
                f"identical message streams, so every room matching rules on both gets "
                f"two agents answering it. Give each connector its own bot account, or "
                f"— on Mattermost only — scope them to different teams."
            )
            continue
        owners = sorted(e.connector_name for e in group if e.owns_dms)
        if len(owners) > 1:
            listed = ", ".join(f"'{n}'" for n in owners)
            conflicts.append(
                f"Connectors {listed} share the bot account {user_id} on {origin} and "
                f"each enable direct messages. Different teams keep their channels "
                f"apart, but a DM has no team and is delivered to every connection the "
                f"account has open, so both would answer the same DM. Enable direct "
                f"messages on exactly one of them."
            )
    return conflicts
