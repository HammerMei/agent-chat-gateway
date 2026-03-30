"""Agent abstraction layer: abstract base for all agent backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from .response import AgentResponse

if TYPE_CHECKING:
    # Avoid circular imports — only used in type annotations
    PermissionHandler = Callable[[str, dict], Awaitable[bool]]
    from ..config import ToolRule
    from ..core.connector import Connector
    from ..core.permission import (
        PermissionBroker,
        PermissionNotifier,
        PermissionRegistry,
    )


@dataclass
class GatewayBrokerConfig:
    """Permission policy settings stored in the backend for gateway broker creation.

    These are agent-level policy decisions (who can run what tools) that belong
    to the agent config, not to the shared gateway runtime state.  The backend
    stores them and forwards them to ``create_gateway_broker()`` so that
    GatewayService never needs to know about per-agent permission details.

    ``owner_allowed_tools`` / ``guest_allowed_tools`` are lists of
    :class:`~gateway.config.ToolRule` parsed from config.  Pass ``None`` to
    use an empty list (all unmatched owner calls require RC approval; guests
    are fully blocked).
    """

    owner_allowed_tools: list = field(default_factory=list)  # list[ToolRule]
    guest_allowed_tools: list = field(default_factory=list)  # list[ToolRule]
    timeout: int = 300
    skip_owner_approval: bool = False  # when True, bypass owner approval for all tool calls


class AgentBackend(ABC):
    """Abstract backend that creates sessions and sends messages to an agent."""

    # ── Backend capability flags ─────────────────────────────────────────────

    @property
    def supports_per_message_env(self) -> bool:
        """Whether this backend uses the ``env`` dict passed to :meth:`send`.

        When ``True`` (the default), :class:`~gateway.core.message_processor.MessageProcessor`
        generates per-message role env (``ACG_ROLE``) and passes it via ``send(env=...)``.
        The backend's subprocess uses these vars for role-aware hook enforcement.

        When ``False``, per-message env is a no-op — the backend either ignores
        ``env`` entirely or requires role to be set at process startup (e.g.
        OpenCode HTTP mode sets ``ACG_ROLE=owner`` on ``opencode serve`` at launch).
        In this case, the processor skips env generation to avoid misleading
        no-op computation.  Guest/owner enforcement is handled entirely by the
        permission broker for such backends.
        """
        return True

    # ── Optional lifecycle hooks (default: no-op) ─────────────────────────────

    async def start(self) -> None:
        """Start any required backend services (e.g. ``opencode serve``).

        Called by :class:`~gateway.service.GatewayService` during startup and
        by :class:`~gateway.agents.session.AgentSession` on ``__aenter__``.
        The default implementation is a no-op — backends that do not require a
        companion process (e.g. :class:`~gateway.agents.claude.adapter.ClaudeBackend`)
        need not override this.

        Implementations must be **idempotent**: calling ``start()`` on an
        already-running backend must return immediately without side-effects.

        Raises:
            RuntimeError: If the backend process fails to start or become healthy.
        """

    async def stop(self) -> None:
        """Stop any background services started by :meth:`start`.

        Called by :class:`~gateway.service.GatewayService` during shutdown and
        by :class:`~gateway.agents.session.AgentSession` on ``__aexit__``.
        The default implementation is a no-op.

        Implementations must be **idempotent**: calling ``stop()`` when already
        stopped must return immediately without raising.
        """

    async def delete_session(self, session_id: str) -> bool:
        """Best-effort deletion of a previously created session.

        Used by watcher startup rollback paths to avoid leaking newly created
        sessions when later setup phases fail (context injection, subscribe,
        etc.).  Returns ``True`` when the backend knows the session was cleaned
        up, ``False`` when deletion is unsupported or could not be confirmed.

        The default implementation returns ``False`` so backends that do not
        support explicit session deletion need not override it.
        """
        return False

    # ── Permission broker factory ──────────────────────────────────────────────

    def create_gateway_broker(
        self,
        registry: "PermissionRegistry",
        notifier: "PermissionNotifier",
        session_room_map: "dict[str, str]",
        session_role_map: "dict[str, str]",
        session_permission_thread_map: "dict[str, str | None]",
    ) -> "PermissionBroker | None":
        """Return a gateway permission broker wired to the shared notification channel.

        Called by :class:`~gateway.service.GatewayService` during startup.
        Returns ``None`` if the backend was constructed without a
        :class:`GatewayBrokerConfig` (i.e. permissions are disabled for this agent).

        The default implementation returns ``None``.  Subclasses override this
        to return the appropriate broker for their permission mechanism:

        - :class:`~gateway.agents.claude.adapter.ClaudeBackend` →
          :class:`~gateway.agents.claude.broker.ClaudePermissionBroker`
        - :class:`~gateway.agents.opencode.adapter.OpenCodeBackend` →
          :class:`~gateway.agents.opencode.broker.OpenCodePermissionBroker`

        Args:
            registry: Shared in-process store for all pending permission requests.
            notifier: Delivers permission messages to chat rooms.
            session_room_map: session_id → room_id for RC notification delivery.
            session_role_map: session_id → "owner"|"guest" for policy enforcement.
            session_permission_thread_map: session_id → RC thread ID (or None).
        """
        return None

    def create_callable_broker(
        self,
        handler: "PermissionHandler",
        timeout_seconds: int,
    ) -> object | None:
        """Return a permission broker that calls ``handler`` for each tool call.

        The returned broker must implement ``start()`` / ``stop()`` coroutines.
        Returning ``None`` means the backend does not support callable permission
        brokers and the handler will be silently ignored.

        The default implementation returns ``None``.  Subclasses override this
        to return the appropriate broker for their permission mechanism:

        - :class:`~gateway.agents.claude.adapter.ClaudeBackend` → ``CallablePermissionBroker``
        - :class:`~gateway.agents.opencode.adapter.OpenCodeBackend` → ``OpenCodeCallablePermissionBroker``

        Args:
            handler: Async callable ``(tool_name: str, tool_input: dict) -> bool``.
            timeout_seconds: Seconds to wait for the handler before auto-denying.
        """
        return None

    # ── Callable broker wiring ─────────────────────────────────────────────

    def attach_callable_broker(self, broker: object) -> None:
        """Wire a callable permission broker into this backend's session path.

        Called by :class:`~gateway.agents.session.AgentSession` after the broker
        is started and before ``create_session()`` runs.  Override in subclasses
        that need broker awareness — for example,
        :class:`~gateway.agents.claude.adapter.ClaudeBackend` patches its own
        ``settings_path`` so ``create_session`` picks up the ``--settings`` flag.

        The default implementation is a no-op — backends whose callable brokers
        are fully independent (e.g. OpenCode SSE) need not override this.
        """

    def detach_callable_broker(self) -> None:
        """Undo any wiring performed by :meth:`attach_callable_broker`.

        Called by :class:`~gateway.agents.session.AgentSession` before the
        broker is stopped.  The default implementation is a no-op.
        """

    # ── Core interface ─────────────────────────────────────────────────────────

    @abstractmethod
    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Start a new agent session and return a persistent session_id.

        Args:
            working_directory: The working directory for the agent subprocess.
            extra_args: Optional extra CLI args passed only during session creation.
            session_title: Optional human-readable title/name for the session
                           (used as --name for Claude, --title for OpenCode).
        """
        ...

    @abstractmethod
    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a message to an existing session and return a normalized AgentResponse.

        Args:
            session_id: The persistent session ID returned by create_session.
            prompt: The text prompt to send.
            working_directory: The working directory for the agent subprocess.
            timeout: Seconds before the call times out.
            attachments: Optional list of local file paths to attach (backend support varies).
            env: Optional extra environment variables to inject into the agent subprocess.
                 Merged on top of the inherited process environment. Used to pass role context
                 (e.g. ACG_ROLE) for hook/plugin enforcement.

        Returns:
            AgentResponse with the agent's reply in ``.text`` and best-effort
            metadata (usage, cost, duration, turns) populated from the backend's
            JSON stream.
        """
        ...
