"""Unit tests for gateway.admin._errors: friendly_error_message and
log_error_response."""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import MagicMock

import httpx

from gateway.admin._errors import friendly_error_message, log_error_response


def _http_status_error(status_code: int, json_body=None, text: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mm.example/api/v4/users")
    if json_body is not None:
        response = httpx.Response(status_code, request=request, json=json_body)
    else:
        response = httpx.Response(status_code, request=request, text=text or "")
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestFriendlyErrorMessage(unittest.TestCase):
    def test_mattermost_message_field_is_used(self):
        exc = _http_status_error(
            400,
            json_body={
                "id": "app.user.save.email_exists.app_error",
                "message": "An account with that email already exists.",
                "detailed_error": "",
                "request_id": "yms88wqc8jr7bj4gdz6od3xf4w",
                "status_code": 400,
            },
        )

        self.assertEqual(
            friendly_error_message(exc), "An account with that email already exists."
        )

    def test_rocketchat_error_field_is_used(self):
        exc = _http_status_error(400, json_body={"success": False, "error": "User not found."})

        self.assertEqual(friendly_error_message(exc), "User not found.")

    def test_fallback_when_no_message_or_error_field(self):
        exc = _http_status_error(
            400,
            json_body={
                "id": "app.user.save.email_exists.app_error",
                "request_id": "yms88wqc8jr7bj4gdz6od3xf4w",
                "status_code": 400,
            },
        )

        result = friendly_error_message(exc)

        self.assertEqual(
            result,
            "Unknown error: request_id: yms88wqc8jr7bj4gdz6od3xf4w, "
            "error_id: app.user.save.email_exists.app_error, error code: 400",
        )

    def test_fallback_with_no_identifying_fields_at_all(self):
        exc = _http_status_error(500, json_body={})

        self.assertEqual(
            friendly_error_message(exc),
            "Unknown error: request_id: N/A, error_id: N/A, error code: 500",
        )

    def test_non_json_body_falls_back_to_status_code_only(self):
        exc = _http_status_error(502, text="<html>Bad Gateway</html>")

        self.assertEqual(friendly_error_message(exc), "Unknown error: error code: 502")

    def test_empty_message_field_falls_through_to_error_field(self):
        exc = _http_status_error(400, json_body={"message": "", "error": "actual error"})

        self.assertEqual(friendly_error_message(exc), "actual error")


class TestLogErrorResponse(unittest.TestCase):
    def test_logs_full_json_body(self):
        exc = _http_status_error(
            400, json_body={"id": "app.user.save.email_exists.app_error", "message": "boom"}
        )
        logger = MagicMock(spec=logging.Logger)

        log_error_response(logger, exc)

        logger.error.assert_called_once()
        args = logger.error.call_args.args
        self.assertEqual(args[0], "%s %s -> HTTP %s\n%s")
        self.assertEqual(args[1], "POST")
        self.assertIn("users", str(args[2]))
        self.assertEqual(args[3], 400)
        logged_body = json.loads(args[4])
        self.assertEqual(logged_body["message"], "boom")

    def test_logs_raw_text_when_body_is_not_json(self):
        exc = _http_status_error(502, text="<html>Bad Gateway</html>")
        logger = MagicMock(spec=logging.Logger)

        log_error_response(logger, exc)

        args = logger.error.call_args.args
        self.assertEqual(args[4], "<html>Bad Gateway</html>")


if __name__ == "__main__":
    unittest.main()
