"""`send` without `--connector` on a multi-connector gateway (#136).

The connector is found from the room through the lifecycle records — by id,
by name, or by watcher handle — in memory. Exactly one serving connector is
used; none or several fall back to the explicit-connector errors.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.control import ControlServer


def _entry(name, *, room_ids=(), room_names=(), handles=()):
    e = MagicMock()
    e.name = name
    e.connector.send_to_room = AsyncMock()
    sm = e.session_manager
    sm.record_for_room = MagicMock(side_effect=lambda rid: object() if rid in room_ids else None)
    sm.resolve_handle = MagicMock(side_effect=lambda h: "R" if h in handles else "")
    sm.list_watchers = MagicMock(return_value=[{"room_name": n, "room_id": "x"} for n in room_names])
    return e


class TestSendFindsTheConnectorFromTheRoom(unittest.IsolatedAsyncioTestCase):

    def _server(self, *entries):
        return ControlServer(entries=list(entries), job_store=MagicMock())

    async def test_the_one_connector_serving_the_room_is_used(self):
        rc = _entry("rc", room_names=("general",))
        mm = _entry("mm", room_names=("sandbox",))

        result = await self._server(rc, mm)._handle_send({"room": "general", "text": "hi"}, None)

        self.assertTrue(result["ok"], result)
        rc.connector.send_to_room.assert_awaited_once()
        mm.connector.send_to_room.assert_not_awaited()

    async def test_a_room_id_and_a_handle_resolve_too(self):
        rc = _entry("rc", room_ids=("R-1",))
        mm = _entry("mm", handles=("mm:eng",))
        server = self._server(rc, mm)

        self.assertTrue((await server._handle_send({"room": "R-1", "text": "hi"}, None))["ok"])
        self.assertTrue((await server._handle_send({"room": "mm:eng", "text": "hi"}, None))["ok"])
        rc.connector.send_to_room.assert_awaited_once()
        mm.connector.send_to_room.assert_awaited_once()

    async def test_a_room_served_by_two_connectors_is_refused_naming_both(self):
        rc = _entry("rc", room_names=("general",))
        mm = _entry("mm", room_names=("general",))

        result = await self._server(rc, mm)._handle_send({"room": "general", "text": "hi"}, None)

        self.assertFalse(result["ok"])
        self.assertIn("'rc'", result["error"])
        self.assertIn("'mm'", result["error"])
        self.assertIn("--connector", result["error"])

    async def test_a_room_nobody_serves_keeps_the_explicit_connector_error(self):
        result = await self._server(_entry("rc"), _entry("mm"))._handle_send({"room": "nowhere", "text": "hi"}, None)

        self.assertFalse(result["ok"])
        self.assertIn("Multiple connectors configured", result["error"])

    async def test_an_explicit_connector_is_not_second_guessed(self):
        rc = _entry("rc", room_names=("general",))
        mm = _entry("mm")

        result = await self._server(rc, mm)._handle_send({"room": "general", "text": "hi"}, "mm")

        self.assertTrue(result["ok"])
        mm.connector.send_to_room.assert_awaited_once()
        rc.connector.send_to_room.assert_not_awaited()

    async def test_a_single_connector_gateway_never_looks_at_records(self):
        rc = _entry("rc")

        result = await self._server(rc)._handle_send({"room": "anything", "text": "hi"}, None)

        self.assertTrue(result["ok"])
        rc.session_manager.record_for_room.assert_not_called()
