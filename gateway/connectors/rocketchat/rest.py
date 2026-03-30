"""Thin Rocket.Chat REST API client for login, post_message, upload_file, and room resolution."""

import asyncio
import logging
import mimetypes
import secrets
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat.rest")


class RoomNotFoundError(Exception):
    """Raised when a room name cannot be resolved because it does not exist.

    Distinct from transport/auth/API failures, which should propagate as-is
    so callers can distinguish a missing room from a broader infrastructure
    problem.
    """


class RocketChatREST:
    """Async REST client for Rocket.Chat API."""

    def __init__(self, server_url: str):
        self.server_url = server_url.rstrip("/")
        self.auth_token: str | None = None
        self.user_id: str | None = None
        self.bot_username: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._client = httpx.AsyncClient(timeout=30.0)
        self._download_client = httpx.AsyncClient(timeout=60.0)
        # Serializes concurrent re-login attempts.  Without this lock, two
        # concurrent requests that both receive a 401 would both call login()
        # simultaneously, race to overwrite auth_token/user_id, and one caller
        # would then retry with a stale token from the other's login response.
        self._relogin_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"RocketChatREST(server_url={self.server_url!r}, "
            f"username={self._username!r}, password=***)"
        )

    async def close(self) -> None:
        """Close shared HTTP clients and release connection pool resources."""
        await self._client.aclose()
        await self._download_client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/{endpoint}"
        sent_token = self.auth_token  # capture before the request
        response = await self._client.request(
            method, url, headers=self._headers(), json=json_data, params=params
        )
        if response.status_code == 401 and self._username:
            async with self._relogin_lock:
                # Inside the lock, check whether the token has already been
                # refreshed by a concurrent coroutine that raced through the
                # same 401 path.  If so, skip re-login and just retry with
                # the new token — calling login() twice would be wasteful and
                # could invalidate the other caller's fresh session.
                if self.auth_token == sent_token:
                    logger.warning("Auth token expired, re-logging in...")
                    if self._username and self._password:
                        await self.login(self._username, self._password)
                    else:
                        raise RuntimeError(
                            "Cannot re-login: username or password not set. "
                            "Ensure login() was called before making requests."
                        )
            response = await self._client.request(
                method, url, headers=self._headers(), json=json_data, params=params
            )
        if not response.is_success:
            logger.error(
                "RC API error %d for %s %s — body: %s",
                response.status_code,
                method,
                endpoint,
                response.text[:500],
            )
        response.raise_for_status()
        return response.json()

    async def login(self, username: str, password: str) -> None:
        """Login and store auth credentials.

        Note: username and password are stored as instance attributes
        (_username, _password) to support automatic re-login when the
        auth token expires (see _request's 401 handling). This is a
        known trade-off for transparent session recovery.
        """
        url = f"{self.server_url}/api/v1/login"
        response = await self._client.post(
            url, json={"user": username, "password": password}
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "success":
            raise RuntimeError(f"Login failed: {data}")

        self.auth_token = data["data"]["authToken"]
        self.user_id = data["data"]["userId"]
        self.bot_username = username
        self._username = username
        self._password = password
        logger.info("Logged in as %s (uid=%s)", username, self.user_id)

    async def post_message(
        self,
        channel: str,
        text: str,
        tmid: str | None = None,
    ) -> None:
        """Post a message to a room by name or ID.

        Args:
            channel : Room ID or name.
            text    : Message body.
            tmid    : Thread root message ID.  When set, the message is posted
                      inside that thread (or starts a new thread if this is the
                      first reply to that message).
        """
        # RC's chat.postMessage has two mutually exclusive schemas:
        #   - without tmid: accepts "channel" (name or ID)
        #   - with tmid:    requires "roomId" (must be room ID, "channel" is rejected)
        if tmid:
            payload: dict = {"roomId": channel, "text": text, "tmid": tmid}
        else:
            payload = {"channel": channel, "text": text}
        result = await self._request("POST", "chat.postMessage", json_data=payload)
        if not result.get("success"):
            raise RuntimeError(f"post_message failed: {result.get('error', result)}")
        logger.info(
            "Posted message to %s%s", channel, f" (thread {tmid})" if tmid else ""
        )

    async def download_file(self, title_link: str, dest_path: str) -> None:
        """Download a file attachment from RC (authenticated) to a local path.

        Accumulates all chunks in memory then writes to a PID-unique temp file
        via asyncio.to_thread, and atomically renames on success.

        Accumulating in memory is safe because the caller (normalize.py) already
        enforces max_file_size_mb before calling this method.  Streaming directly
        to disk with synchronous f.write() inside an async-for loop would block
        the event loop on every write syscall — especially on slow or NFS-mounted
        filesystems.  Offloading the write to a thread keeps the loop responsive.
        """
        url = f"{self.server_url}{title_link}"
        headers = {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
        }
        dest = Path(dest_path)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        # Use a random suffix (not os.getpid()) so that concurrent downloads
        # for the same dest_path each get a unique tmp file and cannot
        # overwrite each other's data before the atomic rename.
        tmp_path = dest.with_name(f"{dest.name}.{secrets.token_hex(8)}.tmp")

        def _stream_download(current_headers: dict[str, str]):
            return self._download_client.stream("GET", url, headers=current_headers)

        async def _collect_chunks(stream_ctx) -> bytes:
            """Collect response chunks into memory."""
            chunks: list[bytes] = []
            async with stream_ctx as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
            return b"".join(chunks)

        def _write_and_rename(data: bytes) -> None:
            with open(tmp_path, "wb") as f:
                f.write(data)
            tmp_path.replace(dest)

        sent_token = self.auth_token  # capture before the request
        try:
            # Phase 1: send the initial request and check for 401.
            # Exit the first context manager *completely* before opening the retry
            # request — keeping a second stream open inside the first context manager
            # would nest two concurrent connections on the same _download_client,
            # which can deadlock if the connection pool is constrained (e.g.
            # max_connections=1) and leaves the first response body unconsumed.
            need_reauth = False
            data: bytes = b""
            async with _stream_download(headers) as first_response:
                if first_response.status_code == 401 and self._username:
                    need_reauth = True
                    # Do NOT read the body — just note that we need to re-auth.
                    # The context manager exits cleanly (httpx discards the body).
                else:
                    first_response.raise_for_status()
                    chunks: list[bytes] = []
                    async for chunk in first_response.aiter_bytes():
                        chunks.append(chunk)
                    data = b"".join(chunks)

            # Phase 2: re-authenticate and retry (first context manager is now fully closed).
            if need_reauth:
                async with self._relogin_lock:
                    if self.auth_token == sent_token:
                        logger.warning(
                            "Auth token expired during download, re-logging in..."
                        )
                        if self._username and self._password:
                            await self.login(self._username, self._password)
                        else:
                            raise RuntimeError(
                                "Cannot re-login: username or password not set. "
                                "Ensure login() was called before making requests."
                            )
                headers = {
                    "X-Auth-Token": self.auth_token or "",
                    "X-User-Id": self.user_id or "",
                }
                data = await _collect_chunks(_stream_download(headers))

            await asyncio.to_thread(_write_and_rename, data)
        except Exception:
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            raise
        logger.info("Downloaded attachment to %s", dest_path)

    async def upload_file(
        self, room_id: str, file_path: str, caption: str = ""
    ) -> None:
        """Upload a file attachment to a room (requires room ID).

        Opens the file as a handle rather than reading all bytes into memory,
        reducing peak RAM for large files.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {file_path}")

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        url = f"{self.server_url}/api/v1/rooms.upload/{room_id}"
        headers = {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
        }
        data = {"msg": caption} if caption else {}

        async def _do_upload() -> httpx.Response:
            # open() is a blocking syscall — offload file reading to a thread to
            # avoid stalling the event loop (especially for cold-cache or NFS files).
            file_bytes = await asyncio.to_thread(path.read_bytes)
            return await self._download_client.post(
                url,
                headers=headers,
                files={"file": (path.name, file_bytes, mime_type)},
                data=data,
            )

        sent_token = self.auth_token  # capture before the request
        response = await _do_upload()
        if response.status_code == 401 and self._username:
            async with self._relogin_lock:
                if self.auth_token == sent_token:
                    logger.warning("Auth token expired during upload, re-logging in...")
                    await self.login(self._username, self._password)
            headers = {
                "X-Auth-Token": self.auth_token or "",
                "X-User-Id": self.user_id or "",
            }
            response = await _do_upload()
        response.raise_for_status()
        result = response.json()

        if not result.get("success"):
            raise RuntimeError(f"upload_file failed: {result.get('error', result)}")
        logger.info("Uploaded file %s to room %s", path.name, room_id)

    async def resolve_room(self, room_name: str) -> dict[str, Any]:
        """Resolve a room name to its info dict.

        Prefix rules:
          - ``@username`` — resolves as a direct message (im.create) with that user.
          - anything else  — tries public channel (channels.info) then private
            group (groups.info).
        """
        if room_name.startswith("@"):
            username = room_name[1:]
            try:
                result = await self._request(
                    "POST", "im.create", json_data={"username": username}
                )
                if result.get("success") and "room" in result:
                    room = result["room"]
                    logger.info("Resolved DM '@%s' -> id=%s", username, room["_id"])
                    return {"_id": room["_id"], "name": room_name, "type": "dm"}
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Failed to open DM with '{username}': {e}") from e
            raise RoomNotFoundError(
                f"DM room for user '{username}' not found (im.create returned unexpected response)"
            )

        # Try public channel
        try:
            result = await self._request(
                "GET", "channels.info", params={"roomName": room_name}
            )
            if result.get("success") and "channel" in result:
                ch = result["channel"]
                logger.info("Resolved channel '%s' -> id=%s", room_name, ch["_id"])
                return {
                    "_id": ch["_id"],
                    "name": ch.get("name", room_name),
                    "type": "channel",
                }
        except httpx.HTTPStatusError as e:
            # RC returns 400 ("Channel_not_found") or 404 when the room does not
            # exist on this endpoint — treat those as "try next endpoint".
            # Any other status (401 auth failure, 500 server error, etc.) is a
            # real infrastructure problem and must NOT be silently swallowed.
            if e.response.status_code not in (400, 404):
                raise

        # Try private group
        try:
            result = await self._request(
                "GET", "groups.info", params={"roomName": room_name}
            )
            if result.get("success") and "group" in result:
                grp = result["group"]
                logger.info("Resolved group '%s' -> id=%s", room_name, grp["_id"])
                return {
                    "_id": grp["_id"],
                    "name": grp.get("name", room_name),
                    "type": "group",
                }
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise

        raise RoomNotFoundError(
            f"Room '{room_name}' not found (tried channels and groups)"
        )
