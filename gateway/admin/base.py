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


def emails_match(existing_email: str, requested_email: str) -> bool:
    """Case/whitespace-insensitive comparison used to decide whether an
    "already exists" collision is plausibly the same identity retrying, or
    an unrelated account that happens to share a username.

    Single source of truth for both concrete admins AND the CLI layer,
    specifically so the decision "is this a safe retry" can't drift between
    them — see UserAlreadyExistsError.identity_matches and
    MattermostAdmin.create_user's team-repair gate, both of which must
    agree on the same answer for the same inputs.

    Two deliberate, NOT provably-safe trade-offs, stated honestly rather
    than as guarantees:

    - An empty ``existing_email`` (Rocket.Chat returns "" when an account
      genuinely has no email on file — see RocketChatAdmin._get_user_or_none)
      is treated as a MATCH, not a mismatch. This fails open: a false
      mismatch turns into a hard CLI failure that blocks this tool's own
      primary re-run-a-seed-script workflow, whereas a false match only
      risks the same limited blast radius idempotent retries already carry
      (repairing team membership / reporting a skip). It is NOT proof the
      account is the same identity — an account with no email on record
      could just as easily be genuinely unrelated.
    - Comparison is case/whitespace-normalized because Mattermost is
      understood to normalize stored email server-side — an exact,
      case-sensitive compare would turn a legitimate idempotent retry
      (same account, different input casing) into a false-positive hard
      failure. This has not been empirically verified against a live
      server.
    """
    if not existing_email:
        return True
    return existing_email.strip().lower() == requested_email.strip().lower()


class UserAlreadyExistsError(AdminError):
    """Raised by create_user() when the username is already taken.

    Carries the pre-existing user so callers that want idempotent
    "ensure this user exists" behavior (e.g. re-run seed scripts, or a
    Btrfs restore that lands on a partially-seeded state) can catch this
    specifically and use .existing instead of treating it as a hard failure.

    ``identity_matches`` (computed by each admin via emails_match(), above)
    tells the caller whether the existing account is plausibly the SAME
    identity retrying, or an unrelated account that happens to share a
    username — the CLI uses this to decide whether "already exists" is a
    safe no-op or a real error worth failing on (see gateway/admin/cli.py).
    Defaults to True so constructing this error without specifying it
    doesn't accidentally manufacture a spurious identity-collision failure.
    """

    def __init__(self, username: str, existing: "AdminUser", *, identity_matches: bool = True):
        super().__init__(f"User '{username}' already exists")
        self.username = username
        self.existing = existing
        self.identity_matches = identity_matches


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


class UserDeactivatedError(AdminError):
    """Raised by create_user() when the username belongs to an existing but
    DEACTIVATED account, so the requested "an active user exists" state was
    not achieved.

    Deliberately subclasses AdminError **directly, and must never subclass
    UserAlreadyExistsError**, however tempting that reads (it is, literally,
    an already-exists case). gateway/admin/cli.py catches
    UserAlreadyExistsError and prints an idempotent "already exists —
    skipping" with exit 0; if this were a subclass, that handler would swallow
    it and reinstate the exact bug this exists to fix — a CLI reporting
    success for an account that cannot log in.

    Reachable using nothing but this tool's own documented commands, in its
    own primary workflow: MattermostAdmin.delete_user() only SOFT-deactivates
    (Mattermost's DELETE /users/{id} sets delete_at rather than removing the
    row), and Mattermost's username lookup still returns soft-deleted
    accounts — so `delete-user bob` followed by `create-user bob ...` while
    reseeding a lab environment would otherwise report a clean skip over a
    dead account.

    Does NOT auto-reactivate: silently resurrecting an account someone
    deliberately deactivated is a bigger surprise than failing loudly, and
    reactivation is a distinct intent that deserves a distinct explicit
    action rather than being a side effect of "create".
    """

    def __init__(self, username: str, existing: "AdminUser"):
        super().__init__(
            f"User '{username}' already exists but is deactivated (id={existing.id}) — "
            "the requested active-user state was not achieved. Reactivate it on the "
            "server, or pick a different username."
        )
        self.username = username
        self.existing = existing


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
    # Whether the platform reports this account as deactivated/soft-deleted.
    # Defaults False and is keyword-safe for every existing construction site.
    # Only Mattermost populates it today (its delete_user() soft-deactivates,
    # and its username lookup still returns such accounts — see
    # UserDeactivatedError); Rocket.Chat's delete_user() hard-deletes, so a
    # deleted RC account simply stops resolving.
    deactivated: bool = False


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
