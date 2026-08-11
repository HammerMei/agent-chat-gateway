"""PlatformAdmin ABC: uniform admin operations over a pluggable RC/MM backend.

Mirrors the Connector ABC pattern (gateway/core/connector.py) deliberately:
a small, generic interface here now pays off later when this logic becomes
the basis for on-demand agent provisioning (create a scoped RC/MM account +
persona, tear it down afterward) — see gateway/admin/__init__.py. Kept
independent of the Connector ABC itself: that one models an already-running
chat session (fetch history, subscribe, post), this one models one-shot
admin operations (create, wire up, delete) — different lifecycles, no
shared behavior worth inheriting.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class AdminError(Exception):
    """Base class for all admin-operation failures raised by this package."""


class UserAlreadyExistsError(AdminError):
    """Raised by create_user() when the username is already taken.

    Carries the pre-existing user so callers that want idempotent
    "ensure this user exists" behavior (e.g. re-run seed scripts, or a
    Btrfs restore that lands on a partially-seeded state) can catch this
    specifically and use .existing instead of treating it as a hard failure.
    """

    def __init__(self, username: str, existing: "AdminUser"):
        super().__init__(f"User '{username}' already exists")
        self.username = username
        self.existing = existing


class ChannelAlreadyExistsError(AdminError):
    """Raised by create_channel() when the channel name is already taken.

    Same idempotency rationale as UserAlreadyExistsError.
    """

    def __init__(self, name: str, existing: "AdminChannel"):
        super().__init__(f"Channel '{name}' already exists")
        self.name = name
        self.existing = existing


class UserNotFoundError(AdminError):
    """Raised when an operation references a username that does not exist."""


class ChannelNotFoundError(AdminError):
    """Raised when an operation references a channel name that does not exist."""


class VerificationError(AdminError):
    """Raised when a create/add call reported success but a read-back check
    could not confirm the change actually took effect.

    Exists because Mattermost is known to return a success-shaped response
    from POST /users without creating the account when EnableUserCreation/
    EnableOpenServer are disabled (see MattermostAdmin.create_user) — a
    silent no-op here would be worse than a loud, specific failure, since
    the whole point of this CLI is to be safe to script against (seed
    scripts, onboarding skill).
    """


@dataclass
class AdminUser:
    id: str
    username: str
    email: str


@dataclass
class AdminChannel:
    id: str
    name: str
    is_private: bool


class PlatformAdmin(ABC):
    """Uniform admin interface, implemented per-platform (RC/MM/...).

    Lifecycle: construct with a profile, ``await connect()`` once, make
    calls, ``await close()`` when done (async context manager support is
    provided via __aenter__/__aexit__ for convenience).
    """

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and resolve any platform-specific prerequisites
        (e.g. Mattermost's team_id). Must be called before any other method."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying HTTP client resources."""

    @abstractmethod
    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        full_name: str | None = None,
        verified: bool = False,
    ) -> AdminUser:
        """Create a user account.

        ``verified`` maps to each platform's email-verified flag (RC's
        ``verified``, Mattermost's ``email_verified``) and defaults to False:
        this tool's primary purpose is provisioning agent accounts, which
        have no real inbox behind them, so claiming a verified email by
        default doesn't make sense. Pass ``verified=True`` explicitly for the
        rare case a created account needs to pass an EnableEmailVerification-
        gated login flow.

        Raises UserAlreadyExistsError if the username is already taken,
        VerificationError if creation appeared to succeed but a read-back
        check couldn't confirm the account exists.
        """

    @abstractmethod
    async def create_channel(self, name: str, *, is_private: bool = False) -> AdminChannel:
        """Create a channel. Raises ChannelAlreadyExistsError if it exists."""

    @abstractmethod
    async def add_user_to_channel(self, username: str, channel_name: str) -> None:
        """Add an existing user to an existing channel.

        Raises UserNotFoundError / ChannelNotFoundError as appropriate.
        """

    @abstractmethod
    async def delete_user(self, username: str) -> None:
        """Delete (or deactivate, if the platform has no hard delete) a user.

        Raises UserNotFoundError if the username does not exist.
        """

    @abstractmethod
    async def delete_channel(self, channel_name: str) -> None:
        """Delete a channel. Raises ChannelNotFoundError if it does not exist."""

    async def __aenter__(self) -> "PlatformAdmin":
        # If connect() raises, __aenter__ never returns normally, and per
        # the async-context-manager protocol Python will NOT call
        # __aexit__ in that case (it's only invoked once __aenter__ has
        # succeeded). Both concrete admins allocate their httpx.AsyncClient
        # instances in their constructors (before connect() ever runs), so
        # without this, every failed connection attempt through `async
        # with SomeAdmin(profile) as admin:` would leak those clients and
        # their connection pools.
        try:
            await self.connect()
        except Exception:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
