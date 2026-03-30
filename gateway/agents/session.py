"""AgentSession: thin wrapper for direct agent scripting without a Connector.

For scripting, testing, and agent-to-agent pipelines where the room/connector
abstraction is unnecessary overhead.  Manages the session lifecycle (create /
send) as an async context manager — no SessionManager, no rooms, no state file.

``__aenter__`` calls ``backend.start()`` (which spawns ``opencode serve`` for
OpenCode backends, or is a no-op for Claude), then creates or resumes a session.
``__aexit__`` stops the backend and any active permission broker.

Usage::

    from gateway.agents.session import AgentSession
    from gateway.agents.claude.adapter import ClaudeBackend
    from gateway.agents.opencode.adapter import OpenCodeBackend

    # Single Claude agent
    async with AgentSession(ClaudeBackend("claude", [], 120), "/my/project") as s:
        reply = await s.send("What files are here?")
        print(reply)

    # Single OpenCode agent — start()/stop() handled automatically
    async with AgentSession(OpenCodeBackend("opencode", [], 120), "/my/project") as s:
        reply = await s.send("Summarize this codebase")
        print(reply)

    # Agent-to-agent pipeline (opencode summarises → claude reviews)
    async with (
        AgentSession(OpenCodeBackend("opencode", [], 120), cwd) as oc,
        AgentSession(ClaudeBackend("claude", [], 120), cwd) as cc,
    ):
        summary = await oc.send("Summarize the codebase")
        review  = await cc.send(f"Review this summary:\\n{summary}")
        print(review)

    # With a programmatic permission handler (callback mechanism: ClaudeBackend only)
    async def my_handler(tool_name: str, tool_input: dict) -> bool:
        return tool_name.lower() == "read"   # approve only Read calls

    async with AgentSession(
        ClaudeBackend("claude", [], 120),
        "/my/project",
        permission_handler=my_handler,
    ) as s:
        reply = await s.send("What files are here?")
"""

from __future__ import annotations

from typing import Awaitable, Callable

from . import AgentBackend
from .response import AgentResponse

# Handler type: receives tool_name + tool_input, returns True to allow.
PermissionHandler = Callable[[str, dict], Awaitable[bool]]


class AgentSession:
    """Thin wrapper around AgentBackend for scripting and agent-to-agent pipelines.

    Manages session lifecycle without requiring a Connector, room concept, or
    SessionManager.  Use as an async context manager — ``__aenter__`` calls
    ``create_session`` and stores the ``session_id``; subsequent ``send()``
    calls reuse that ID.

    Attributes:
        session_id: Assigned after entering the context manager; ``None`` before.

    Example::

        async with AgentSession(backend, "/my/project", timeout=120) as session:
            reply = await session.send("Hello!")
            print(reply)
    """

    def __init__(
        self,
        backend: AgentBackend,
        working_directory: str,
        timeout: int = 120,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
        session_id: str | None = None,
        permission_handler: PermissionHandler | None = None,
    ) -> None:
        """
        Args:
            backend: The agent backend to drive (ClaudeBackend, OpenCodeBackend, …).
            working_directory: Working directory passed to the agent subprocess.
            timeout: Seconds before a ``send()`` call raises ``asyncio.TimeoutError``.
            extra_args: Extra CLI args forwarded only to ``create_session``
                        (e.g. ``["--agent", "assistance"]``).
            session_title: Optional human-readable title for the session
                           (``--name`` for Claude, ``--title`` for opencode).
            session_id: Attach to an existing session instead of creating a new one.
                        When provided, ``create_session`` is skipped entirely and
                        the given ID is used for all ``send()`` calls.
            permission_handler: Async callable invoked for each tool call that
                requires permission.  Signature:
                ``async (tool_name: str, tool_input: dict) -> bool``.
                Return ``True`` to allow, ``False`` to deny.
                Supported by all backends that implement
                ``create_callable_broker()`` — currently ClaudeBackend (via
                PreToolUse HTTP hook) and OpenCodeBackend (via SSE).
                When provided, a broker is started on ``__aenter__`` and
                stopped on ``__aexit__``.
        """
        self._backend = backend
        self._cwd = working_directory
        self._timeout = timeout
        self._extra_args = extra_args
        self._session_title = session_title
        self.session_id: str | None = session_id
        self._permission_handler = permission_handler
        self._broker: object | None = None  # CallablePermissionBroker, if started

    async def __aenter__(self) -> "AgentSession":
        """Start the backend, create a new session (or reuse an existing one).

        Calls ``backend.start()`` first — a no-op for Claude, but spawns
        ``opencode serve`` for OpenCode backends.

        If a ``permission_handler`` was provided, starts a
        ``CallablePermissionBroker`` and tells the backend to wire it in via
        ``attach_callable_broker()`` — any backend-specific setup (e.g. Claude's
        ``settings_path`` patching) is handled inside the backend, not here.
        """
        await self._backend.start()

        if self._permission_handler is not None:
            broker = self._backend.create_callable_broker(
                self._permission_handler,
                timeout_seconds=self._timeout,
            )
            if broker is not None:
                await broker.start()
                self._broker = broker
                self._backend.attach_callable_broker(broker)

        if self.session_id is None:
            self.session_id = await self._backend.create_session(
                self._cwd,
                extra_args=self._extra_args,
                session_title=self._session_title,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        """Stop the permission broker (if any) and shut down the backend.

        Uses try/finally to guarantee ``backend.stop()`` runs even if the
        broker teardown fails — prevents orphaned backend processes (e.g. a
        running ``opencode serve`` left behind after a broker stop error).
        """
        try:
            if self._broker is not None:
                self._backend.detach_callable_broker()
                await self._broker.stop()  # type: ignore[union-attr]
                self._broker = None
        finally:
            await self._backend.stop()

    async def send(
        self,
        prompt: str,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a message to the agent and return a normalized AgentResponse.

        Args:
            prompt: The text prompt to send.
            attachments: Optional local file paths to attach (backend support varies).
            env: Optional extra environment variables for the agent subprocess.
                 Merged on top of the inherited process environment.

        Returns:
            AgentResponse with the agent's reply in ``.text`` and best-effort
            metadata (usage, cost, duration, turns).  ``str(response)`` returns
            ``response.text`` for convenient pipeline use.

        Raises:
            RuntimeError: If called before entering the context manager.
            asyncio.TimeoutError: If the agent exceeds the configured ``timeout``.
        """
        if self.session_id is None:
            raise RuntimeError(
                "AgentSession is not active — use it as an async context manager:\n"
                "    async with AgentSession(...) as session:\n"
                "        reply = await session.send(...)"
            )
        return await self._backend.send(
            self.session_id,
            prompt,
            self._cwd,
            self._timeout,
            attachments=attachments,
            env=env,
        )
