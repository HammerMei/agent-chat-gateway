"""pytest fixtures for E2E tests.

Session-scoped:
    rc_setup     — runs setup.py; verifies RC is reachable
    mm_setup     — runs mm_setup.py; verifies Mattermost is reachable
    acg          — waits for ACG Docker container to be ready
    mm_connected — asserts the mm-e2e CONNECTOR itself came up inside ACG

Function-scoped:
    test_client  — RCClient logged in as test_user
    admin_client — RCClient logged in as admin
    e2e_room     — parameterized: "dm" (→ OpenCode) or "channel" (→ Claude Code)

Both platforms live in one ACG container with one connector each, and MM's
boot failures split in a way worth knowing before reading a red suite:

* An unresolvable **team** or an unusable identity is *fatal to the whole
  gateway* — `resolve_team` raises out of `connect()`, and the daemon treats a
  connect-phase failure as fatal and exits 1. So this one shows up as the
  `acg` fixture failing for every test, RC's included, not as an MM-specific
  failure. Sharing one container buys a shared blast radius.
* Anything after that — a socket that opens and then drops, an allow-list that
  rejects the poster — leaves the gateway up and answering, and an MM test's
  only symptom is a 120-second timeout indistinguishable from a slow agent.

`mm_connected` covers the second class, and it is why it greps for a line
logged *after* `resolve_team()` rather than checking the process is alive.
Same taste as `_container_missing` below — name the real failure at second
zero instead of inferring it from a wait that never ends.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Allow importing rc_client and setup from the same e2e directory
sys.path.insert(0, str(Path(__file__).parent))
import mm_setup as _mm
from mm_client import MMClient
from rc_client import RCClient
from setup import RC_URL
from setup import setup as _run_setup

# ── Constants ─────────────────────────────────────────────────────────────────

E2E_DIR = Path(__file__).parent
COMPOSE_FILE = str(E2E_DIR / "docker-compose.yml")
ACG_CONTAINER = "acg-e2e"
BOT_USERNAME = "acg_bot"
ACG_READY_TIMEOUT = 180  # seconds — includes OpenCode pre-warm (~60s)
ACG_READY_INTERVAL = 5

# Must match the connector name in tests/e2e/acg-config/config.yaml. Pinned by
# tests/unit/test_e2e_mm_wiring.py so a rename fails without Docker.
MM_CONNECTOR_NAME = "mm-e2e"
ACG_LOG_PATH = "/root/.agent-chat-gateway/gateway.log"
# Logged by MattermostConnector.connect() — and logged *after* resolve_team(),
# so its presence also proves the configured team resolved.
MM_CONNECTED_MARKER = "MattermostConnector connected to"


# ── Session fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def rc_setup() -> dict[str, Any]:
    """Run E2E setup and return config dict.

    Expects RC to already be running (started by Makefile / CI).
    If RC is not reachable, the fixture fails with a clear message.
    """
    rc_url = os.environ.get("E2E_RC_URL", RC_URL)
    try:
        return _run_setup(rc_url)
    except RuntimeError as exc:
        pytest.fail(
            f"E2E setup failed — is RC running?\n"
            f"  Start with: make e2e-up\n"
            f"  Error: {exc}"
        )


@pytest.fixture(scope="session")
def acg(rc_setup: dict[str, Any]) -> None:
    """Wait for the ACG Docker container to be ready, then warm up agents.

    Expects the container to already be started (by Makefile / CI).
    Polls `docker exec acg-e2e agent-chat-gateway status` until it succeeds
    or times out.

    After ACG reports ready, sends a warm-up ping to both the DM (OpenCode)
    and the team channel (Claude Code).  OpenCode starts its subprocess lazily
    on the first request, so without this warm-up the first real test can time
    out waiting for the cold-start initialisation to complete.
    """
    print(f"\n[acg] Waiting for ACG container '{ACG_CONTAINER}' ...", flush=True)
    _wait_for_acg(timeout=ACG_READY_TIMEOUT, interval=ACG_READY_INTERVAL)
    print("[acg] ACG is ready.", flush=True)

    # ── Warm-up: trigger both agents so their subprocesses are initialised ────
    _warmup_agents(rc_setup)

    yield
    # Do NOT stop the container here — Makefile / CI handles lifecycle.
    # This lets tests be re-run quickly without restarting ACG.


def _warmup_agents(rc_setup: dict[str, Any]) -> None:
    """Send a warm-up ping to DM (OpenCode) and channel (Claude Code).

    OpenCode initialises its subprocess lazily on the first message, which can
    take 60–90 s.  This function fires a simple 'pong' request at both agents
    and waits up to 120 s for each response — ensuring they are fully warmed up
    before the test suite starts.  Failures here are non-fatal (a warning is
    printed) so that individual tests can still provide actionable failure info.
    """
    rc_url = rc_setup["rc_url"]
    warmup_timeout = 120

    with RCClient(rc_url) as c:
        c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])

        # ── DM → OpenCode ─────────────────────────────────────────────────────
        try:
            print("[acg] Warming up OpenCode (DM) ...", flush=True)
            dm_room_id = c.get_dm_room_id(BOT_USERNAME)
            before_ts = int(time.time() * 1000)
            c.post_message(dm_room_id, "respond with exactly the single word 'ready'")
            c.poll_for_message(
                dm_room_id,
                before_ts,
                predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                timeout=warmup_timeout,
                room_type="dm",
            )
            print("[acg] OpenCode (DM) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: OpenCode warm-up failed: {exc}", flush=True)

        # ── Channel → Claude Code ──────────────────────────────────────────────
        try:
            print("[acg] Warming up Claude Code (channel) ...", flush=True)
            ch = c.get_channel(rc_setup["claude_channel"])
            if ch:
                before_ts = int(time.time() * 1000)
                c.post_message(
                    ch["_id"],
                    f"@{BOT_USERNAME} respond with exactly the single word 'ready'",
                )
                c.poll_for_message(
                    ch["_id"],
                    before_ts,
                    predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                    timeout=warmup_timeout,
                    room_type="channel",
                )
                print("[acg] Claude Code (channel) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: Claude Code warm-up failed: {exc}", flush=True)


def _container_missing(name: str) -> bool:
    """Is there no container by this name at all (as opposed to one that is
    merely not ready yet)?"""
    result = subprocess.run(
        ["docker", "container", "inspect", name],
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def _wait_for_acg(timeout: float, interval: float) -> None:
    """Poll docker exec until agent-chat-gateway status returns 0.

    "Not there at all" and "there but still starting" get different
    treatment. Waiting the full timeout for a container that does not exist
    is pure loss — and worse than loss, because the eventual failure reads
    like a readiness problem when the real answer is "you never brought the
    stack up". That cost a real debugging session: the suite was run against
    a stack where only MongoDB and Rocket.Chat had been started, so this
    polled a nonexistent container for three minutes and then errored every
    single test.
    """
    if _container_missing(ACG_CONTAINER):
        pytest.fail(
            f"Container '{ACG_CONTAINER}' does not exist — the stack is not up.\n"
            "Run 'make e2e-up' first (it starts MongoDB + Rocket.Chat, creates "
            "the RC accounts, then starts ACG). 'make e2e-test' only runs the "
            "suite; it does not bring anything up.\n"
            "If e2e-up itself failed partway, 'make e2e-dump' writes the full "
            "container logs to ./e2e-logs."
        )

    deadline = time.monotonic() + timeout
    last_output = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", ACG_CONTAINER, "agent-chat-gateway", "status"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_output = (result.stdout + result.stderr).strip()
        # A container that vanishes mid-wait (crash-looping, or torn down)
        # is the same actionable case as never having existed.
        if _container_missing(ACG_CONTAINER):
            pytest.fail(
                f"Container '{ACG_CONTAINER}' disappeared while waiting for it "
                "to become ready — it likely crashed on startup. "
                "'make e2e-dump' writes the full logs to ./e2e-logs."
            )
        time.sleep(interval)

    # On timeout, dump ACG logs for debugging
    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", ACG_CONTAINER],
        capture_output=True,
        text=True,
    ).stdout
    pytest.fail(
        f"ACG did not become ready within {timeout}s.\n"
        f"Last status output: {last_output}\n"
        f"Container logs (last 50 lines):\n{logs}"
    )


# ── Mattermost fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def mm_setup() -> dict[str, Any]:
    """Bootstrap Mattermost and return the config dict.

    Expects the container to already be running (started by Makefile / CI).
    Re-running is harmless: `mm_setup.setup()` is idempotent.
    """
    mm_url = os.environ.get("E2E_MM_URL", _mm.MM_URL)
    try:
        return _mm.setup(mm_url)
    except Exception as exc:
        pytest.fail(
            f"Mattermost E2E setup failed — is Mattermost running?\n"
            f"  Start with: make e2e-up\n"
            f"  Error: {type(exc).__name__}: {exc}"
        )


@pytest.fixture(scope="session")
def mm_connected(acg: None, mm_setup: dict[str, Any]) -> None:
    """Fail fast unless the mm-e2e connector actually came up inside ACG.

    `acg` becoming ready means the *gateway* is answering, which it can do
    with the MM socket down — the fatal half of MM's boot failures (see the
    module docstring) has already taken the `acg` fixture with it by the time
    this runs, so what is left to check is the half that does not. Depending
    on this fixture converts that half from a 120-second timeout in whichever
    MM test ran first into a named failure before any message is sent.
    """
    log = subprocess.run(
        ["docker", "exec", ACG_CONTAINER, "sh", "-c", f"cat {ACG_LOG_PATH}"],
        capture_output=True,
        text=True,
    ).stdout
    if MM_CONNECTED_MARKER in log:
        return

    # Give the operator the lines that explain it rather than "not found".
    mm_lines = [
        line
        for line in log.splitlines()
        if "attermost" in line or MM_CONNECTOR_NAME in line
    ]
    pytest.fail(
        f"The '{MM_CONNECTOR_NAME}' connector never reported connecting "
        f"(no {MM_CONNECTED_MARKER!r} in {ACG_LOG_PATH}).\n"
        "The gateway is up, so this is the connector specifically: a bad "
        "account, an unresolvable team, or a websocket that could not open. "
        "Most likely the MM bootstrap did not run before ACG started — "
        "'make e2e-up' does both in order; starting the container by hand "
        "does not.\n"
        "Mattermost-related log lines:\n  "
        + ("\n  ".join(mm_lines[-25:]) if mm_lines else "(none at all)")
    )


@pytest.fixture(scope="session")
def mm_test_client(mm_setup: dict[str, Any]) -> MMClient:
    """MM client logged in as the human test user."""
    c = MMClient(mm_setup["mm_url"])
    c.login(mm_setup["test_user_username"], mm_setup["test_user_password"])
    yield c
    c.close()


@pytest.fixture(scope="session")
def mm_admin_client(mm_setup: dict[str, Any]) -> MMClient:
    """MM client logged in as the system admin.

    Membership questions need this one: a non-member cannot read even its own
    membership row (403), so establishing that the bot is NOT in a channel
    requires admin eyes — see MMClient.is_channel_member.
    """
    c = MMClient(mm_setup["mm_url"])
    c.login(mm_setup["admin_username"], mm_setup["admin_password"])
    yield c
    c.close()


# ── Function-scoped fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as test_user.

    Session-scoped to avoid RC's login rate limit (429) when running many tests.
    The httpx.Client maintains a persistent connection pool for the session.
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])
    yield c
    c.close()


@pytest.fixture(scope="session")
def admin_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as admin.

    Session-scoped to avoid RC's login rate limit (429).
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["admin_username"], rc_setup["admin_password"])
    yield c
    c.close()


@pytest.fixture(scope="session", params=["dm", "channel"])
def e2e_room(
    request: pytest.FixtureRequest,
    rc_setup: dict[str, Any],
    test_client: RCClient,
) -> dict[str, Any]:
    """Parameterized room fixture: runs each test twice.

    "dm"      → DM room between test_user and acg_bot → OpenCode agent
    "channel" → #acg-e2e-claude public channel        → Claude Code agent

    Returned dict:
        id:             RC room _id
        type:           "dm" or "channel"
        agent:          "opencode" or "claude"
        name:           human-readable label
        mention_prefix: "" for DM, "@acg_bot " for channel
                        (channel messages need @bot mention to be processed)
    """
    if request.param == "dm":
        room_id = test_client.get_dm_room_id(BOT_USERNAME)
        return {
            "id": room_id,
            "type": "dm",
            "agent": "opencode",
            "name": f"DM with {BOT_USERNAME}",
            "mention_prefix": "",
        }
    else:
        ch = test_client.get_channel(rc_setup["claude_channel"])
        if ch is None:
            pytest.fail(
                f"Channel '#{rc_setup['claude_channel']}' not found. "
                "Run 'make e2e-up' first."
            )
        return {
            "id": ch["_id"],
            "type": "channel",
            "agent": "claude",
            "name": rc_setup["claude_channel"],
            "mention_prefix": f"@{BOT_USERNAME} ",
        }
