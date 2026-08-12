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
def readback_after_write(what_the_write_reported: str):
    """Wrap a verification read-back that follows a write which REPORTED success.

    Every create/delete in this package writes, then reads back to confirm the
    change landed — precisely because these platforms have been observed to
    report success WITHOUT applying the write (mattermost/mattermost#6644). If
    the read-back itself fails with an API error, two things used to go wrong:

    - The operator saw httpx's generic ``Client error '403 Forbidden' for url
      '...'  For more information check: https://developer.mozilla.org/...``
      instead of the platform's own message, because the wrapping bypassed
      cli._run()'s httpx.HTTPStatusError arm (which calls
      friendly_error_message) in favour of its generic Exception arm.
    - Nothing indicated that the write had reported success first. A bare
      ``Error: You do not have the appropriate permissions.`` after an
      apparently-successful account creation reads exactly like "nothing
      happened".

    What the message must NOT do — and originally did — is assert that the
    change definitely landed and tell the operator not to re-run. The read-back
    is the only thing that could have established that, and it just failed, so
    the outcome is genuinely UNKNOWN. Combined with the false-success behavior
    above, the confident wording was actively harmful in exactly the scenario
    this wrapper exists for: a Mattermost create that returns 2xx without
    creating the account, followed by a 403 read-back, would have told the
    operator the account exists and must not be re-created. So the message now
    reports what is actually known (the write reported success, verification
    could not confirm it) and directs the operator to check server state before
    deciding — rather than making the decision for them on an unverified
    premise.

    Only httpx.HTTPStatusError is caught — a VerificationError raised by the
    read-back's own logic (e.g. "delete_at is still unset") is already specific
    and passes through untouched.
    """
    try:
        yield
    except httpx.HTTPStatusError as e:
        raise VerificationError(
            f"{what_the_write_reported}, but the follow-up verification request "
            f"failed: {friendly_error_message(e)} — so whether the change "
            "actually landed is UNKNOWN. Check the current state on the server "
            "before deciding whether to re-run: these platforms can report "
            "success without applying a write, and re-running may not be "
            "idempotent if it did land."
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
