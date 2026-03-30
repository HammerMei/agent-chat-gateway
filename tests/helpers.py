"""Shared test helpers for agent-chat-gateway test suite."""

from __future__ import annotations

import unittest
from unittest.mock import patch

# Patch load_state/save_state globally so tests never touch live state files.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


class IsolatedTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _patch_load_state.start()
        _patch_save_state.start()
        self.addCleanup(_patch_load_state.stop)
        self.addCleanup(_patch_save_state.stop)
