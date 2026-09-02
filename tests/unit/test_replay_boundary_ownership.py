"""A named replay window does not spend the room's own boundary — everywhere.

The rule (§2.2): `replay_room_since(room_id, after_ts=...)` is a caller asking
about a window it froze earlier — a startup down-window, or the interval a
parked room owes. The room's `replay_boundary` is a *different* mark, set by
its own hand-back accounting to say "a message below here was refused and must
come back". A replay that read someone else's window has no claim to spend it.

This file exists because that rule was written down correctly and then applied
to one discharge site out of two per connector — and each connector happened to
guard the arm the other missed, so no single reading of either file looked
wrong. Two of these tests are behavioural (they catch *this* site) and one
walks the surface (it catches the *next* one), because neither kind substitutes
for the other.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.connector import HistoryPage, Room


def _rc_connector(messages, *, was_full=False):
    from gateway.connectors.rocketchat.connector import (
        RocketChatConnector,
        _RoomSubscription,
    )

    c = RocketChatConnector.__new__(RocketChatConnector)
    c._rooms = {}
    c._ws = MagicMock()
    c._ws.subscription_statuses = {}
    c._rest = MagicMock()
    c._rest.is_room_member = AsyncMock(return_value=True)
    c._rest.get_room_history_page = AsyncMock(
        return_value=HistoryPage(messages=messages, raw_count=len(messages),
                                limit=200 if not was_full else len(messages)))
    c._REPLAY_HISTORY_COUNT = 200 if not was_full else len(messages)
    sub = _RoomSubscription(room=Room(id="r1", name="eng", type="channel"))
    sub.last_processed_ts = "500"
    # What a hand-back left behind: a promise to read below this mark later.
    sub.replay_boundary = "100"
    sub.boundary_claims = 1
    c._rooms["r1"] = sub
    c._on_raw_ddp_message = AsyncMock(return_value=True)
    return c, sub


def _mm_connector(messages, *, was_full=False):
    from gateway.connectors.mattermost.connector import (
        MattermostConnector,
        _ChannelState,
    )

    c = MattermostConnector.__new__(MattermostConnector)
    c._channels = {}
    c._membership_gen = {}
    c._rest = MagicMock()
    c._rest.get_room_history_page = AsyncMock(
        return_value=HistoryPage(messages=messages, raw_count=len(messages),
                                limit=200 if not was_full else len(messages)))
    c._REPLAY_HISTORY_COUNT = 200 if not was_full else len(messages)
    state = _ChannelState(room=Room(id="c1", name="eng", type="channel"))
    state.last_processed_ts = "500"
    state.replay_boundary = "100"
    state.boundary_claims = 1
    c._channels["c1"] = state
    c._synthesize_decoded_for_replay = MagicMock(side_effect=lambda p: {"post": p})
    c._on_posted_event = AsyncMock()
    return c, state


class TestANamedWindowNeverSpendsTheRoomsBoundary(unittest.IsolatedAsyncioTestCase):
    """Behavioural, per connector, per arm — the empty page and the dispatched
    batch are two different discharge sites and both had to be checked."""

    async def test_rc_empty_page(self):
        c, sub = _rc_connector([])
        await c.replay_room_since("r1", after_ts="400")
        self.assertEqual(sub.replay_boundary, "100")

    async def test_rc_dispatched_batch(self):
        """The common one: this arm runs on every recreation, because the
        message that triggered it is itself inside the fetched window."""
        c, sub = _rc_connector([{"_id": "m1", "msg": "hi"}])
        await c.replay_room_since("r1", after_ts="400")
        self.assertEqual(sub.replay_boundary, "100")

    async def test_mm_empty_page(self):
        c, state = _mm_connector([])
        await c.replay_room_since("c1", after_ts="400")
        self.assertEqual(state.replay_boundary, "100")

    async def test_mm_dispatched_batch(self):
        c, state = _mm_connector([{"id": "m1", "create_at": 600}])
        await c.replay_room_since("c1", after_ts="400")
        self.assertEqual(state.replay_boundary, "100")

    async def test_rc_own_window_still_discharges(self):
        """The guard must not disable the rule it qualifies: a reconnect replay
        reads the room's own mark and *is* entitled to spend it."""
        c, sub = _rc_connector([{"_id": "m1", "msg": "hi"}])
        await c.replay_room_since("r1")
        self.assertIsNone(sub.replay_boundary)

    async def test_mm_own_window_still_discharges(self):
        c, state = _mm_connector([{"id": "m1", "create_at": 600}])
        await c.replay_room_since("c1")
        self.assertIsNone(state.replay_boundary)


class TestEveryDischargeSiteIsGuarded(unittest.TestCase):
    """Walks the surface rather than a list, so the *next* discharge site is
    covered before it is written.

    The behavioural tests above catch a site that exists and is wrong; only
    this one catches a site that does not exist yet. The two are not
    substitutes — that lesson cost a full review round earlier in this series.
    """

    def _sites(self, method):
        """Every `discharge_boundary(...)` call in the method, with the set of
        names its enclosing conditions test."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        found = []

        def walk(node, guards):
            if isinstance(node, ast.If):
                names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
                # The test itself is walked too, and with its own names in
                # scope: both connectors spell the guard and the call in one
                # boolean expression (`if not external_window and not
                # state.discharge_boundary(...)`), so a walker that only
                # descended into the body would report every real site as
                # absent — which is exactly what the first version of this test
                # did, and it read as "nothing discharges" rather than as a
                # broken walker.
                walk(node.test, guards | names)
                for child in node.body:
                    walk(child, guards | names)
                for child in node.orelse:
                    # An `elif`/`else` is still inside the chain's decision.
                    walk(child, guards | names)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, guards)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "discharge_boundary":
                found.append(guards)

        walk(tree, frozenset())
        return found

    def test_no_unguarded_discharge_in_either_replay(self):
        from gateway.connectors.mattermost.connector import MattermostConnector
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        for cls in (RocketChatConnector, MattermostConnector):
            with self.subTest(connector=cls.__name__):
                sites = self._sites(cls.replay_room_since)
                self.assertTrue(sites, "replay_room_since must discharge somewhere")
                for guards in sites:
                    self.assertIn(
                        "external_window", guards,
                        f"{cls.__name__}.replay_room_since spends the room's "
                        "replay boundary without asking whose window it read — "
                        "a caller-named window is not this room's mark to spend",
                    )


if __name__ == "__main__":
    unittest.main()


class TestAMattermostReplayRevalidatesMembership(unittest.IsolatedAsyncioTestCase):
    """A removal while the WebSocket was down produces no `user_removed` event, and
    the bot's token can still read a public channel it has left — so a reconnect
    replay dispatched a kicked channel's backlog into the old watcher. The gap
    `core/replay_window.py` recorded; closed with the by-id lookup the connector
    now has (Codex, PR #140 round 2)."""

    async def test_a_channel_the_account_left_is_not_replayed(self):
        c, state = _mm_connector([{"id": "p1", "create_at": 450, "message": "x"}])
        c._resolved_channel = AsyncMock(return_value=None)   # the connector's final "not ours"

        await c.replay_room_since("c1", after_ts="400")

        c._on_posted_event.assert_not_awaited()
        self.assertTrue(state.membership_lost, "marked for reconciliation to reclaim")

    async def test_a_lookup_failure_is_unknown_not_a_removal(self):
        """A network blip must not invent a kick: the replay proceeds as before."""
        c, state = _mm_connector([{"id": "p1", "create_at": 450, "message": "x"}])
        c._resolved_channel = AsyncMock(side_effect=OSError("network"))

        await c.replay_room_since("c1", after_ts="400")

        self.assertFalse(state.membership_lost)
        c._on_posted_event.assert_awaited()

    async def test_a_member_channel_replays_as_before(self):
        c, state = _mm_connector([{"id": "p1", "create_at": 450, "message": "x"}])
        c._resolved_channel = AsyncMock(return_value=("O", "eng", ()))

        await c.replay_room_since("c1", after_ts="400")

        c._on_posted_event.assert_awaited()

