"""Friendly error formatting + detailed logging for RC/MM API failures.

RocketChatREST/MattermostREST's shared ``_request()`` raises
``httpx.HTTPStatusError`` via ``response.raise_for_status()`` on any non-2xx
response. httpx's own ``str(exc)`` is generic (e.g. "Client error '400 Bad
Request' for url '...'") and never surfaces the platform's actual error
message (e.g. Mattermost's "An account with that email already exists.") —
that only exists in the response body. This module extracts it for display,
and separately preserves the full raw body in a log file so nothing is lost
even though the console message is now short.
"""

import contextlib
import json
import logging

import httpx

from gateway.admin.base import VerificationError


@contextlib.contextmanager
def readback_after_write(what_already_happened: str):
    """Wrap a verification read-back that follows an ALREADY-SUCCEEDED write.

    Every create/delete in this package writes, then reads back to confirm the
    change landed (the platforms have been observed to report success without
    applying it). If that read-back itself fails with an API error, two things
    used to go wrong at once:

    - The operator saw httpx's generic ``Client error '403 Forbidden' for url
      '...'  For more information check: https://developer.mozilla.org/...``
      instead of the platform's own message, because the wrapping bypassed
      cli._run()'s httpx.HTTPStatusError arm (which calls
      friendly_error_message) in favour of its generic Exception arm.
    - Worse, nothing said the write had ALREADY been applied. A bare
      ``Error: You do not have the appropriate permissions.`` after a
      successful account creation reads exactly like "nothing happened",
      inviting the operator to re-run a command that already did its job.

    So the message states both: the platform's real error, and that the write
    is already done. Only httpx.HTTPStatusError is caught — a VerificationError
    raised by the read-back's own logic (e.g. "delete_at is still unset") is
    already specific and passes through untouched.
    """
    try:
        yield
    except httpx.HTTPStatusError as e:
        raise VerificationError(
            f"{what_already_happened}, but the follow-up verification request "
            f"failed: {friendly_error_message(e)} — NOTE: the change above has "
            "already been applied on the server, so re-running is not required "
            "and may not be idempotent."
        ) from e


def friendly_error_message(exc: httpx.HTTPStatusError) -> str:
    """Extract a human-readable message from a platform API error response.

    Tries Mattermost's shape (``{"message": ...}``) then Rocket.Chat's
    (``{"error": ...}``); falls back to a compact "Unknown error: ..."
    summary built from whatever identifying fields are present, so a
    failure never surfaces as httpx's generic, cause-free error string.
    """
    response = exc.response
    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if message:
            return message
        request_id = body.get("request_id", "N/A")
        error_id = body.get("id", body.get("errorType", "N/A"))
        return (
            f"Unknown error: request_id: {request_id}, error_id: {error_id}, "
            f"error code: {response.status_code}"
        )

    return f"Unknown error: error code: {response.status_code}"


def log_error_response(logger: logging.Logger, exc: httpx.HTTPStatusError) -> None:
    """Log the full raw response body for an HTTPStatusError.

    Preserves what friendly_error_message() intentionally leaves out (full
    body, request id, url) for troubleshooting, without cluttering the
    console-facing message.
    """
    response = exc.response
    try:
        body_text = json.dumps(response.json(), indent=2)
    except ValueError:
        body_text = response.text
    logger.error(
        "%s %s -> HTTP %s\n%s",
        exc.request.method, exc.request.url, response.status_code, body_text,
    )
