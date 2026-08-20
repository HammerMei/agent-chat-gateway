"""The Mattermost E2E suite's assumptions about its own config, pinned here.

Everything asserted below is something `tests/e2e/test_mm_*.py` depends on and
cannot check itself, because checking needs the stack it is describing. The
same reasoning as `test_e2e_watcher_names.py`, which exists because the
schedule E2E test was silently passing rule names where watcher handles were
required — caught by hand after a cutover, not by a test.

The load-bearing one is `TestTheGlobStillClaimsTheOutsideChannel`. The
membership test's whole value rests on the rule matching a channel the bot has
not joined, so that the absence of a reply can only be explained by the
absence of an event. Narrow `acg-e2e-mm-*` to the member channel and that test
keeps passing while testing nothing at all — the worst failure mode a test
can have, and one no E2E run would report.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

from gateway.core.watcher_manager import RoomRef, watcher_label
from gateway.core.watcher_rule import RoomKind, RuleMatch

_REPO = Path(__file__).resolve().parents[2]
_E2E = _REPO / "tests" / "e2e"
_E2E_CONFIG = _E2E / "acg-config" / "config.yaml"
_CONFTEST = _E2E / "conftest.py"

# The E2E helpers import each other as top-level modules.
sys.path.insert(0, str(_E2E))
import mm_setup  # noqa: E402

MM_CHANNEL_RULE = "e2e-mm-channel"
MM_DM_RULE = "e2e-mm-dm"


def _config() -> dict:
    return yaml.safe_load(_E2E_CONFIG.read_text())


def _rules() -> dict[str, dict]:
    return {
        entry["name"]: entry
        for entry in (_config().get("watchers") or [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def _conftest_constant(name: str) -> str:
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', _CONFTEST.read_text(), re.M)
    assert match, f"{name} not found in {_CONFTEST.name}"
    return match.group(1)


class TestTheMMConnectorIsDeclaredUnderTheNameTheTestsUse(unittest.TestCase):
    def test_the_conftest_connector_name_is_declared_in_the_config(self):
        declared = {
            c["name"]: c.get("type")
            for c in (_config().get("connectors") or [])
            if isinstance(c, dict) and isinstance(c.get("name"), str)
        }
        name = _conftest_constant("MM_CONNECTOR_NAME")
        self.assertIn(name, declared)
        self.assertEqual(
            "mattermost",
            declared[name],
            f"connector {name!r} is not a Mattermost connector — the MM tests "
            "build handles with this name and would silently target the wrong "
            "platform",
        )

    def test_both_mm_rules_are_on_that_connector(self):
        """Via the template, not inline — `e2e-default` pins `rc-e2e`, so a
        rule that forgot `inherits: e2e-mm-default` would land on Rocket.Chat
        and match nothing."""
        by_name = {r.name: r for r in _loaded_config().watcher_rules}
        name = _conftest_constant("MM_CONNECTOR_NAME")
        for rule in (MM_CHANNEL_RULE, MM_DM_RULE):
            with self.subTest(rule=rule):
                self.assertIn(rule, by_name)
                self.assertEqual(name, by_name[rule].connector)


def _loaded_config():
    """The E2E config through the real loader.

    `working_directory` points inside the container, so a straight load fails
    on this machine for a reason that has nothing to do with what is being
    tested. Rewriting it to a path that exists everywhere keeps the assertions
    about rules rather than about the host.
    """
    import tempfile

    from gateway.config import GatewayConfig

    patched = _E2E_CONFIG.read_text().replace(
        "/root/.agent-chat-gateway/work", tempfile.gettempdir()
    )
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(patched)
    return GatewayConfig.from_file(str(path))


class TestTheGlobStillClaimsTheOutsideChannel(unittest.TestCase):
    """`tests/e2e/test_mm_membership_delivery.py` is only a test while this
    holds."""

    def setUp(self):
        self.rule = {r.name: r for r in _loaded_config().watcher_rules}[MM_CHANNEL_RULE]

    def test_it_claims_the_member_channel(self):
        self.assertEqual(
            RuleMatch.CLAIMED,
            self.rule.match(mm_setup.MEMBER_CHANNEL, RoomKind.CHANNEL),
        )

    def test_it_also_claims_the_channel_the_bot_never_joins(self):
        self.assertEqual(
            RuleMatch.CLAIMED,
            self.rule.match(mm_setup.OUTSIDE_CHANNEL, RoomKind.CHANNEL),
            f"the rule no longer matches {mm_setup.OUTSIDE_CHANNEL!r}. "
            "test_mm_membership_delivery.py would still pass, and would prove "
            "nothing: a reply is absent because the rule declined, not because "
            "Mattermost delivered no event. Either widen the glob back or "
            "delete that test — do not leave it passing.",
        )

    def test_it_does_not_reach_the_rocket_chat_channels(self):
        """Both platforms live in one config; the MM glob must not shadow RC
        room names of a similar shape."""
        for rc_room in ("acg-e2e-claude", "acg-e2e-permission", "acg-e2e-outside"):
            with self.subTest(room=rc_room):
                self.assertEqual(
                    RuleMatch.NO_MATCH, self.rule.match(rc_room, RoomKind.CHANNEL)
                )


class TestTheHandleFormatsTheMMTestsAssertOn(unittest.TestCase):
    """`test_mm_membership_delivery.py` builds `<connector>:<channel>` by hand
    to check `list --all`, the same coupling `test_e2e_watcher_names.py` pins
    for the schedule test."""

    def test_a_channel_handle_is_connector_colon_the_channel_slug(self):
        """The slug, not the display name: the MM connector fills RoomRef.name
        from the event's `channel_name`."""
        self.assertEqual(
            f"mm-e2e:{mm_setup.MEMBER_CHANNEL}",
            watcher_label(
                "mm-e2e",
                RoomRef(
                    id="whatever",
                    kind=RoomKind.CHANNEL,
                    name=mm_setup.MEMBER_CHANNEL,
                    participants=(),
                ),
            ),
        )

    def test_a_dm_handle_is_connector_colon_dm_colon_counterpart(self):
        self.assertEqual(
            f"mm-e2e:dm:{mm_setup.TEST_USER_USERNAME}",
            watcher_label(
                "mm-e2e",
                RoomRef(
                    id="whatever",
                    kind=RoomKind.DM,
                    name="",
                    participants=(mm_setup.TEST_USER_USERNAME,),
                ),
            ),
        )


# The other half of the membership setup — the human in that channel, the bot
# out of it — is deliberately NOT pinned here. Asserting it would mean matching
# mm_setup.py's source text, which breaks on a reformat and passes on a rename;
# and the property is already asserted where it can be OBSERVED rather than
# read, in test_mm_membership_delivery.py's preconditions, against the live
# server and with a message naming the fix.


if __name__ == "__main__":
    unittest.main()
