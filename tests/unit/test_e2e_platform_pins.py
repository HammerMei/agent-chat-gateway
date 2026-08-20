"""The E2E stack must run the platform versions the design was verified on.

`docs/design/dynamic-watcher-design.md` §6 records platform behaviour the
runtime *depends on* — for Rocket.Chat, the `__my_messages__` subscription
being accepted, `args[1]` carrying `roomParticipant`/`roomType`/`roomName`,
system messages arriving so a `t`-field filter is required; for Mattermost,
delivery tracking membership rather than readability (§6.2) and channel names
being unique only within a team (§6.3) — and it states the versions those were
probed against.

The E2E compose pinned Rocket.Chat 6.12 from the commit that first added the
suite, and nobody revisited it when delivery switched to the subscribe-all
stream. So the suite was validating the new runtime against a server version
none of §6's findings had been checked on, and nothing failed to say so —
that is what this pins, now for both platforms rather than one.

Every assertion here runs without Docker, which is the point: the E2E suite
itself cannot check any of them, because checking needs the stack it is
describing.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "tests" / "e2e" / "docker-compose.yml"
_DESIGN = _REPO / "docs" / "design" / "dynamic-watcher-design.md"
_RC_CLIENT = _REPO / "tests" / "e2e" / "rc_client.py"

# One row per platform, so adding a third connector's E2E stack is one entry
# rather than another copy of the parsing.
#
#   image_pattern  — matches the compose `image:` line, capturing the tag
#   design_pattern — matches the design doc's "Versions tested:" sentence
#   floor          — major version the repo's own code assumes (see the test)
_PLATFORMS = (
    {
        "label": "Rocket.Chat",
        "image_pattern": r"^\s*image:\s*rocket\.chat:(\S+)\s*$",
        "design_pattern": r"Versions tested:.*?Rocket\.Chat\s+([0-9]+(?:\.[0-9]+)*)",
        # Several things here are written for RC 8+: the two-step upload below,
        # the connector's own version dispatch.
        "floor": 8,
    },
    {
        "label": "Mattermost",
        "image_pattern": r"^\s*image:\s*mattermost/mattermost-team-edition:(\S+)\s*$",
        "design_pattern": r"Versions tested:.*?Mattermost\s+([0-9]+(?:\.[0-9]+)*)",
        "floor": 11,
    },
)


def _pinned_version(image_pattern: str, label: str) -> str:
    match = re.search(image_pattern, _COMPOSE.read_text(), re.M)
    assert match, f"no {label} image pin found in the E2E compose"
    return match.group(1)


def _design_verified_version(design_pattern: str, label: str) -> str:
    # "Versions tested: **Rocket.Chat 8.5.1** and **Mattermost 11.7.0**."
    match = re.search(design_pattern, _DESIGN.read_text())
    assert match, f"the design doc no longer states a tested {label} version"
    return match.group(1)


class TestE2EPinsMatchTheDesign(unittest.TestCase):
    def test_each_compose_pin_is_the_version_the_design_was_verified_on(self):
        for platform in _PLATFORMS:
            with self.subTest(platform=platform["label"]):
                self.assertEqual(
                    _design_verified_version(platform["design_pattern"], platform["label"]),
                    _pinned_version(platform["image_pattern"], platform["label"]),
                    f"the E2E stack runs a different {platform['label']} than the "
                    "one §6 of dynamic-watcher-design.md was probed against — "
                    "either re-probe (scripts/probe_a1_rc.py / "
                    "scripts/probe_a2_mm.py) and update the design doc, or move "
                    "the compose pin. A silent gap here means E2E is validating "
                    "the runtime against platform behaviour nobody verified.",
                )

    def test_each_pin_clears_the_floor_this_repo_assumes(self):
        """A separate, weaker check: dropping under these majors needs the code
        written against them revisited, not just the pin changed."""
        for platform in _PLATFORMS:
            with self.subTest(platform=platform["label"]):
                major = int(_pinned_version(platform["image_pattern"], platform["label"]).split(".")[0])
                self.assertGreaterEqual(major, platform["floor"])


class TestE2EClientDoesNotUseRemovedEndpoints(unittest.TestCase):
    """`POST /api/v1/rooms.upload/{rid}` was REMOVED in Rocket.Chat 8.0.0.
    The product connector version-dispatches around that; the E2E client does
    not, so it must use the two-step flow outright."""

    def setUp(self):
        self.source = _RC_CLIENT.read_text()

    def test_rooms_upload_is_not_called(self):
        """Checked against the actual request CALLS, not the file's text.

        The docstring explaining why this endpoint is avoided necessarily
        names it — twice, including the URL form — and a guard that tripped
        on its own explanation would only teach the next person to delete
        the explanation."""
        requested: list[str] = []
        for node in ast.walk(ast.parse(self.source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in {"post", "get"}):
                continue
            for arg in node.args:
                segment = ast.get_source_segment(self.source, arg) or ""
                requested.append(segment)
        self.assertTrue(requested, "no HTTP calls found — did the client move?")
        offenders = [r for r in requested if "rooms.upload" in r]
        self.assertEqual(
            [],
            offenders,
            "rooms.upload was removed in RC 8.0.0 — use rooms.media + "
            "rooms.mediaConfirm (see this client's upload_file docstring)",
        )

    def test_both_halves_of_the_two_step_upload_are_present(self):
        """A file uploaded via rooms.media alone sits unconfirmed and never
        appears in the room, so the confirm call is not optional."""
        for endpoint in ("rooms.media/", "rooms.mediaConfirm/"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.source)


if __name__ == "__main__":
    unittest.main()
