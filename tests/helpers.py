"""Shared test helpers.

**Fixtures live here by default** (see `CLAUDE.md`, *Test Fixtures Are Shared By
Default*). A local copy in one suite is the exception and needs a reason in the
same breath — "it is small" is not one, because small fixtures duplicate just as
expensively. Adding one attribute to `WatcherLifecycle` once broke nineteen
tests at a stroke, every one of them a hand-built object missing a field no real
instance can lack.

The builders below run the **real constructors** and take keyword overrides for
the collaborators a test needs to substitute. That is the point: an object built
through `__init__` cannot be in a state no code path can produce, so it fails
where it is wrong rather than three layers away.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.core.config import CoreConfig
from gateway.core.connector import Room
from gateway.core.session_manager import SessionManager

# Patch load_state/save_state globally so tests never touch live state files.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


class IsolatedTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _patch_load_state.start()
        _patch_save_state.start()
        self.addCleanup(_patch_load_state.stop)
        self.addCleanup(_patch_save_state.stop)


class MockAgentBackend(AgentBackend):
    """The shared agent double: canned responses, recorded calls, optional error.

    A superset of the three near-identical copies it replaces, so no caller
    loses anything::

        agent = MockAgentBackend(responses=["Hello!", "World!"])
        agent.side_effect = asyncio.TimeoutError   # make send() raise
        agent.sent_messages[0]["prompt"]
        agent.created_sessions[0]["working_directory"]
    """

    def __init__(self, responses=None, default_response: str = "mock reply") -> None:
        self._responses = list(responses or [])
        self._default_response = default_response
        self.side_effect: type[Exception] | None = None

        # Captured call records for assertions.
        self.created_sessions: list[dict] = []
        self.sent_messages: list[dict] = []
        self.deleted_sessions: list[str] = []

        self._session_counter = 0

    async def create_session(
        self, working_directory, extra_args=None, session_title=None
    ) -> str:
        self._session_counter += 1
        session_id = f"mock-session-{self._session_counter:04d}"
        self.created_sessions.append(
            {
                "session_id": session_id,
                "working_directory": working_directory,
                "extra_args": extra_args,
                "session_title": session_title,
            }
        )
        return session_id

    async def send(
        self, session_id, prompt, working_directory, timeout,
        attachments=None, env=None, append_system_prompt_file=None,
    ) -> AgentResponse:
        self.sent_messages.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "working_directory": working_directory,
                "timeout": timeout,
                "attachments": attachments,
                "env": env,
            }
        )
        if self.side_effect is not None:
            raise self.side_effect()
        text = self._responses.pop(0) if self._responses else self._default_response
        return AgentResponse(text=text)

    async def ensure_durable_instructions(self, *a, **kw):
        """Skip the default send()-based fallback, so watcher startup does not
        consume a canned response. A test exercising context injection should
        override this rather than rely on it (see
        tests/integration/test_injected_context_builder.py)."""
        return None


class CleanupTrackingAgent(MockAgentBackend):
    """`MockAgentBackend` that also *confirms* session deletion.

    Kept separate on purpose. `AgentBackend.delete_session` returns `False` by
    default — "deletion is unsupported or could not be confirmed" — and startup
    rollback branches on that: an unconfirmed delete keeps the session and its
    injection flag for the next attempt. Folding confirmation into the generic
    double would quietly move every rollback test onto the other branch while
    still passing, which is exactly the "reuse must not fuse two independent
    things" case in CLAUDE.md.
    """

    async def delete_session(self, session_id: str) -> bool:
        self.deleted_sessions.append(session_id)
        return True


def make_rc_config(
    server_url: str = "http://chat.example.com",
    username: str = "bot",
    password: str = "pw",
    name: str = "rc",
    owners=None,
    **overrides,
):
    """A minimal `RocketChatConfig` — lifted from `test_connector.py` when the
    wake-path suite became its second consumer."""
    from gateway.config import AttachmentConfig
    from gateway.connectors.rocketchat.config import RocketChatConfig

    return RocketChatConfig(
        server_url=server_url,
        username=username,
        password=password,
        name=name,
        owners=owners or ["alice"],
        attachments=AttachmentConfig(cache_dir_global="/tmp/rc-cache"),
        **overrides,
    )


def make_watcher(room="script", name=None, connector="script", agent="default", **kw):
    """A `WatcherConfig` with the fields most tests do not care about filled in."""
    return WatcherConfig(
        name=name or room, connector=connector, room=room, agent=agent, **kw
    )


async def start_watcher(lifecycle, wc, state=None, **kw):
    """Start a watcher from a `WatcherConfig`, for tests of the start machinery.

    The port of the deleted `_start_watcher`: resolve the configured room
    reference through the connector, then enter the one start path
    (`start_watcher_in_room`). Production has no name-resolving start left —
    the manager's create/recreate and `_resume_record` all arrive holding a
    room — but the start machinery's own behaviours (session provisioning,
    identity comparison, history handoff, rollback) are start-path
    properties, and these suites exercise them without needing the routing
    stack above.
    """
    room = await lifecycle._connector.resolve_room(wc.room)
    await lifecycle.start_watcher_in_room(wc, state, room, **kw)


def make_rule(room="script", name=None, connector="default", agent="default", **kw):
    """A `WatcherRule` naming one literal room — the post-cutover analogue of
    `make_watcher` for suites that start watchers at boot: on an eager
    connector (Script) the eager-start loop materializes it at `run_once`,
    creating a watcher named `<connector>-<room>` (the derived label)."""
    from gateway.core.room_pattern import RoomPattern
    from gateway.core.watcher_rule import RoomMatcher, WatcherRule

    return WatcherRule(
        name=name or f"rule-{room}", connector=connector, agent=agent,
        rooms=RoomMatcher(include=(RoomPattern(room),)), **kw,
    )


def make_rule_derived_record(
    name="w1",
    *,
    room_id=None,
    idle_days=15,
    expire_days=15,
    connector="rc",
    agent="default",
    session_id="sess-1",
    **kw,
):
    """A `WatcherState` as the watcher manager persists it: frozen rule AND
    materialized config, so `config_from_record` accepts it. Added when the
    membership-events suite became the second place to need one (the idle-sweep
    suite's local builder predates the config snapshot and pins TTL arithmetic,
    which never reads it)."""
    from gateway.core.state import WatcherState, backend_identity

    defaults = {
        "watcher_name": name,
        "session_id": session_id,
        "room_id": room_id or f"room-{name}",
        "room_type": "channel",
        "connector": connector,
        "agent": agent,
        "rule_name": "eng",
        "rule": {"session_idle_days": idle_days, "session_expire_days": expire_days},
        "config": {"name": name, "connector": connector, "room": "", "agent": agent},
        # Faithful to what a real start writes (round 19): a record WITH a
        # session always carries the backend identity it was minted against —
        # an empty one now reads "unverifiable" at reclaim and skips the
        # agent-bound cleanup, which is the corruption case, not the default.
        "backend_identity": backend_identity(
            AgentConfig().type, AgentConfig().working_directory),
    }
    defaults.update(kw)
    return WatcherState(**defaults)


def make_core_config(timeout: int = 10, agents=None, **kw):
    """A `CoreConfig` with one agent, which is what most tests need."""
    return CoreConfig(
        agents=agents or {"default": AgentConfig(timeout=timeout)},
        **kw,
    )


def make_manager(
    connector=None,
    agent=None,
    *,
    timeout: int = 10,
    permission_registry=None,
    state_name: str = "default",
    agents=None,
    # The name the single default agent is registered under. Not a
    # `default_agent:` fallback — that config key is gone; this only decides the
    # key in the `agents` dict a rule would have to name explicitly.
    agent_name: str = "default",
    config=None,
    **kw,
) -> SessionManager:
    """A `SessionManager` wired to one connector and one agent.

    Replaces three near-identical copies that differed only by which of
    `timeout`, `permission_registry` and `state_name` they exposed.
    """
    from gateway.connectors.script import ScriptConnector

    connector = ScriptConnector() if connector is None else connector
    agent = MockAgentBackend() if agent is None else agent
    return SessionManager(
        connector,
        agents or {agent_name: agent},
        config or make_core_config(timeout=timeout),
        state_name=state_name,
        permission_registry=permission_registry,
        **kw,
    )


async def settle_routing_tasks(connector):
    """Wait until every spawned routing episode has finished AND been discarded.

    Not a bare `while set: gather` — that livelocks: awaiting an already-done
    task returns **without yielding**, and the `discard` runs in a done-callback
    that needs loop time it then never gets. The whole test spins as one giant
    callback (caught on Python 3.13, where the runner's debug mode reported a
    single 120-second callback; 3.11 only ever dodged it by timing). The
    `sleep(0)` is the yield that lets the callbacks drain the set.
    """
    import asyncio

    while connector._routing_tasks:
        await asyncio.gather(*connector._routing_tasks)
        await asyncio.sleep(0)


def make_bare_session_manager(**attrs):
    """A `SessionManager` built without `__init__`, every collaborator a double.

    For method-level tests that pin one SessionManager method against mocks —
    the real constructor builds real collaborators, which is a different kind
    of test (`make_manager`). Sets every attribute `__init__` sets, because a
    hand-built subset describes an object no code path produces, and the first
    method to read an unset field fails several layers from the cause — the
    `WatcherLifecycle.__new__` lesson, re-learned on SessionManager when
    `_sweep` arrived and seven tests across two files broke at once.

    A drift test (`test_session_manager_commands`) compares this against a
    real instance, so the next `__init__` field fails there, once, with a
    message that says what to do.
    """
    from unittest.mock import AsyncMock

    from gateway.core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._connector = MagicMock()
    mgr._connector.connect = AsyncMock()
    mgr._connector.start_inbound = AsyncMock()
    mgr._connector.disconnect = AsyncMock()
    mgr._connector_name = "default"
    mgr._dispatcher = MagicMock()
    mgr._injector = MagicMock()
    mgr._state_store = MagicMock()
    mgr._lifecycle = MagicMock()
    mgr._lifecycle.sync_watchers = AsyncMock(return_value=[])
    mgr._lifecycle.stop_all = AsyncMock()
    mgr._lifecycle.drain_verbs = AsyncMock()
    mgr._lifecycle.pause_watcher = AsyncMock()
    mgr._lifecycle.resume_watcher = AsyncMock()
    mgr._lifecycle.reset_watcher = AsyncMock()
    # No rules → no creation path and no sweep; tests that exercise either
    # override these two together, the way __init__ gates them together.
    mgr._watcher_manager = None
    mgr._sweep = None
    mgr._cancel_jobs = None
    mgr._watcher_rules = []
    for name, value in attrs.items():
        setattr(mgr, name, value)
    return mgr


def make_processor(agent=None, **overrides):
    """A real `MessageProcessor` with a double for every collaborator.

    Runs the real constructor and takes keyword overrides for whatever the
    test substitutes — the same shape as `make_lifecycle`, added when the
    idle-clock suite became the third file to need one.
    """
    from gateway.core.message_processor import MessageProcessor

    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.notify_typing = AsyncMock()
    defaults = {
        "session_id": "ses_001",
        "room": Room(id="room_1", name="test-room"),
        "working_directory": "/tmp",
        "watcher_id": "test-watcher",
        "connector": connector,
        "agent": agent or MockAgentBackend(),
        "config": make_core_config(),
        "agent_name": "default",
    }
    defaults.update(overrides)
    return MessageProcessor(**defaults)


def make_lifecycle(**overrides):
    """A real `WatcherLifecycle` with a double for every collaborator.

    Built through `__init__`, deliberately: the thirteen hand-rolled
    `WatcherLifecycle.__new__(...)` fixtures this replaces each assigned their
    own subset of attributes, so every new field broke all of them at once and
    each was free to omit one and produce a state no code path can reach.

    Pass a keyword for anything the test actually exercises; everything else is
    a `MagicMock` that will fail loudly if the test unexpectedly depends on it.
    """
    from gateway.core.watcher_lifecycle import WatcherLifecycle

    defaults = {
        "connector": MagicMock(),
        "agents": {},
        "config": make_core_config(),
        # `load()` must return a real empty mapping, not a MagicMock. A bare
        # mock's `.get(name)` is truthy, so `sync_watchers` reads every watcher
        # as paused, starts none of them, and returns no errors — a lifecycle
        # test built on that passes without exercising startup at all, which is
        # the precise failure this file exists to prevent.
        "state_store": MagicMock(load=MagicMock(return_value={})),
        "dispatcher": MagicMock(),
        "injector": MagicMock(),
        "permission_registry": None,
        "maps": MagicMock(),
    }
    defaults.update(overrides)
    return WatcherLifecycle(**defaults)


# ── Installing records into a WatcherLifecycle ────────────────────────────────
#
# `WatcherLifecycle` keys its records and processors by ROOM ID and finds them
# by name through an index. A test that wrote `lifecycle._states[name] = r`
# produced a state no code path can reach (a record under the wrong key, with
# no name index) — and there were nine of them across six files. These go
# through the lifecycle's own single write points instead.


def install_record(lifecycle, record, *, as_name=None, processor=None):
    """Install `record` the way the lifecycle does, optionally with a resident
    processor. `as_name` is the key the test used to write under — kept so a
    fixture whose key disagreed with the record's own name fails loudly here
    rather than silently testing a record nothing could find."""
    if as_name is not None and as_name != record.watcher_name:
        raise AssertionError(
            f"fixture installed {record.watcher_name!r} under key {as_name!r}")
    lifecycle._install(record)
    if processor is not None:
        lifecycle._set_processor(record.watcher_name, processor)
    return record


def register_processor(lifecycle, name, processor):
    """Make `processor` resident for the watcher `name` (record must exist)."""
    lifecycle._set_processor(name, processor)
    return processor


def pop_processor(lifecycle, name):
    return lifecycle._pop_processor(name)


def evict_record(lifecycle, name):
    """Drop the record and any processor under `name`, as a reclaim would."""
    lifecycle._pop_processor(name)
    return lifecycle._uninstall(name)
