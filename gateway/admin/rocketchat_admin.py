"""Rocket.Chat implementation of PlatformAdmin.

Composes a RocketChatREST instance rather than extending it, for the same
reason as MattermostAdmin — see that module's docstring.

Two RC-specific asymmetries vs. Mattermost worth flagging up front:
  - No PAT/token-only auth path: RocketChatREST only supports username/
    password login today, so RocketChatAdmin requires both in the profile
    (unlike MattermostAdmin, which prefers a token). See connect().
  - Public/private channels are genuinely different resources in RC's API
    (channels.* vs groups.*), not one resource with a type flag like
    Mattermost — every method here dispatches on is_private accordingly.
  - users.delete is a real hard delete (unlike Mattermost's soft-delete
    deactivation) — see delete_user().
"""

import logging

import httpx

from gateway.admin._logging import quiet_expected_error
from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    ChannelNotFoundError,
    PlatformAdmin,
    UserAlreadyExistsError,
    UserNotFoundError,
    VerificationError,
)
from gateway.admin.config import AdminConfigError, AdminProfile
from gateway.connectors.rocketchat.rest import RocketChatREST
from gateway.connectors.rocketchat.rest import logger as _rc_rest_logger

logger = logging.getLogger("agent-chat-gateway.admin.rocketchat")

# RC treats "not found" on info-lookup endpoints as a 400, not a 404 (same
# behavior RocketChatREST.resolve_room already works around).
_NOT_FOUND_STATUSES = (400, 404)


class RocketChatAdmin(PlatformAdmin):
    def __init__(self, profile: AdminProfile):
        if not (profile.username and profile.password):
            raise AdminConfigError(
                f"Profile '{profile.name}': RocketChatAdmin requires 'username' "
                "and 'password' — token-only auth isn't supported yet "
                "(RocketChatREST has no token-mode constructor path)."
            )
        self.profile = profile
        self._rest = RocketChatREST(server_url=profile.server_url)

    async def connect(self) -> None:
        await self._rest.login(self.profile.username, self.profile.password)  # type: ignore[arg-type]

    async def close(self) -> None:
        await self._rest.close()

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        full_name: str | None = None,
        verified: bool = False,
    ) -> AdminUser:
        existing = await self._get_user_or_none(username)
        if existing is not None:
            raise UserAlreadyExistsError(username, existing=existing)

        payload = {
            "name": full_name or username,
            "email": email,
            "password": password,
            "username": username,
            # verified defaults to False (see PlatformAdmin.create_user
            # docstring — agent accounts have no real inbox behind them).
            # requirePasswordChange is unconditionally False regardless: an
            # admin-created service/seed account still needs to be usable
            # unattended, and that's an unrelated RC default (forced
            # password reset on first login), not an email-verification one.
            "verified": verified,
            "requirePasswordChange": False,
        }
        result = await self._rest._request("POST", "users.create", json_data=payload)
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat user creation for '{username}' failed: {result.get('error', result)}"
            )

        created = await self._get_user_or_none(username)
        if created is None:
            raise VerificationError(
                f"Rocket.Chat reported user '{username}' created but a "
                "read-back lookup could not find it."
            )
        return created

    async def create_channel(self, name: str, *, is_private: bool = False) -> AdminChannel:
        existing = await self._get_channel_or_none(name)
        if existing is not None:
            raise ChannelAlreadyExistsError(name, existing=existing)

        endpoint = "groups.create" if is_private else "channels.create"
        result = await self._rest._request("POST", endpoint, json_data={"name": name})
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat channel creation for '{name}' failed: {result.get('error', result)}"
            )

        verified = await self._get_channel_or_none(name)
        if verified is None:
            raise VerificationError(
                f"Rocket.Chat reported channel '{name}' created but a "
                "read-back lookup could not find it."
            )
        return verified

    async def add_user_to_channel(self, username: str, channel_name: str) -> None:
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Rocket.Chat user '{username}' not found")
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Rocket.Chat channel '{channel_name}' not found")

        endpoint = "groups.invite" if channel.is_private else "channels.invite"
        result = await self._rest._request(
            "POST", endpoint, json_data={"roomId": channel.id, "userId": user.id}
        )
        if not result.get("success"):
            raise VerificationError(
                f"Adding '{username}' to channel '{channel_name}' failed: "
                f"{result.get('error', result)}"
            )

        members_endpoint = "groups.members" if channel.is_private else "channels.members"
        if not await self._is_channel_member(members_endpoint, channel.id, username):
            raise VerificationError(
                f"Added '{username}' to channel '{channel_name}' but a "
                "read-back membership check did not find them in the member list."
            )

    async def delete_user(self, username: str) -> None:
        """Hard-delete a user account (unlike Mattermost, which soft-deletes)."""
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Rocket.Chat user '{username}' not found")

        result = await self._rest._request(
            "POST", "users.delete", json_data={"userId": user.id}
        )
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat user deletion for '{username}' failed: {result.get('error', result)}"
            )

        still_there = await self._get_user_or_none(username)
        if still_there is not None:
            raise VerificationError(
                f"Deleted user '{username}' but a read-back lookup still finds it."
            )

    async def delete_channel(self, channel_name: str) -> None:
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Rocket.Chat channel '{channel_name}' not found")

        endpoint = "groups.delete" if channel.is_private else "channels.delete"
        result = await self._rest._request(
            "POST", endpoint, json_data={"roomId": channel.id}
        )
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat channel deletion for '{channel_name}' failed: "
                f"{result.get('error', result)}"
            )

        still_there = await self._get_channel_or_none(channel_name)
        if still_there is not None:
            raise VerificationError(
                f"Deleted channel '{channel_name}' but a read-back lookup still finds it."
            )

    async def _get_user_or_none(self, username: str) -> AdminUser | None:
        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "users.info", params={"username": username}
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _NOT_FOUND_STATUSES:
                return None
            raise
        if not result.get("success"):
            return None
        user = result["user"]
        email = ""
        emails = user.get("emails") or []
        if emails:
            email = emails[0].get("address", "")
        return AdminUser(id=user["_id"], username=user["username"], email=email)

    async def _get_channel_or_none(self, name: str) -> AdminChannel | None:
        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "channels.info", params={"roomName": name}
                )
            if result.get("success") and "channel" in result:
                ch = result["channel"]
                return AdminChannel(id=ch["_id"], name=ch.get("name", name), is_private=False)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _NOT_FOUND_STATUSES:
                raise

        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "groups.info", params={"roomName": name}
                )
            if result.get("success") and "group" in result:
                grp = result["group"]
                return AdminChannel(id=grp["_id"], name=grp.get("name", name), is_private=True)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _NOT_FOUND_STATUSES:
                raise

        return None

    async def _is_channel_member(self, members_endpoint: str, room_id: str, username: str) -> bool:
        """Paginate through channels.members/groups.members looking for
        ``username``, rather than trusting the default single page.

        RC's default ``count`` for these list endpoints is 50 — a channel
        with more members than that would otherwise make this check miss a
        real member and raise a false VerificationError on a successful
        invite. ``count=0`` is documented to mean "all", but only when the
        server's API_Allow_Infinite_Count setting is enabled, which isn't
        guaranteed — explicit pagination works regardless of that setting.
        """
        offset = 0
        page_size = 100
        while True:
            result = await self._rest._request(
                "GET", members_endpoint,
                params={"roomId": room_id, "offset": offset, "count": page_size},
            )
            members = result.get("members", [])
            if any(m.get("username") == username for m in members):
                return True
            if not members:
                return False
            total = result.get("total", offset + len(members))
            offset += len(members)
            if offset >= total:
                return False
