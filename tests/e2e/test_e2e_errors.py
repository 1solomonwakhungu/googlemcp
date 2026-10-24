"""Google failures surface as MCP tool errors and never leak credentials."""

from __future__ import annotations

import importlib.util
import json
import logging
from typing import Any

import pytest
from e2e_harness import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    FakeGoogle,
    account_store,
    assert_no_secrets,
    call_tool,
    make_server,
    require_adapter,
    result_text,
    strict_mode,
)
from google.oauth2.credentials import Credentials

from googlemcp.auth.credentials import TOKEN_URI, CredentialProvider
from googlemcp.config import load_settings

TASKLISTS = r"/tasks/v1/users/@me/lists$"


def _google_error(status: int, reason: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "message": message,
            "status": "PERMISSION_DENIED" if status == 403 else "ERROR",
            "errors": [{"reason": reason, "message": message}],
        }
    }


def _call_tasks(google: FakeGoogle, **server_kwargs: Any) -> Any:
    mcp = make_server(load_settings(products=["tasks"]), google, **server_kwargs)
    return call_tool(mcp, "tasks_list_tasklists")


def test_scope_error_is_a_tool_error_with_recovery_hint() -> None:
    google = FakeGoogle().on(
        "GET",
        TASKLISTS,
        _google_error(403, "insufficientPermissions", "Request had insufficient scopes."),
        status=403,
    )
    result = _call_tasks(google)
    text = result_text(result)
    assert result.is_error
    assert "Google API error 403 (insufficientPermissions)" in text
    assert "googlemcp auth login" in text
    assert "tasks.googleapis.com" not in text, "request URI must not be echoed"
    assert_no_secrets([text])


LEAKY_MESSAGE = (
    "Tasks API has not been used in project 123 before. Enable it by visiting "
    "https://console.developers.google.com/apis/api/tasks.googleapis.com/overview"
    "?project=123 then retry. Request: tasks.googleapis.com/tasks/v1/users/@me/lists"
    "?access_token=leaky-token-value"
)


def _require_redactor() -> None:
    if importlib.util.find_spec("googlemcp.redaction") is None:
        message = "googlemcp.redaction is not on this branch"
        if strict_mode():
            pytest.fail(f"{message}; docs/security.md claims URL redaction")
        pytest.skip(message)


def _assert_no_leak(text: str) -> None:
    assert "https://" not in text
    assert "console.developers.google.com" not in text
    assert "tasks.googleapis.com/tasks" not in text
    assert "leaky-token-value" not in text
    assert_no_secrets([text])


@pytest.mark.parametrize(("status", "reason"), [(400, "badRequest"), (500, "backendError")])
def test_urls_in_google_error_messages_are_redacted(status: int, reason: str) -> None:
    """docs/security.md: Google's message is shown with URLs redacted."""
    _require_redactor()
    google = FakeGoogle().on(
        "GET", TASKLISTS, _google_error(status, reason, LEAKY_MESSAGE), status=status
    )
    text = result_text(_call_tasks(google))
    assert f"Google API error {status} ({reason})" in text
    assert "has not been used in project 123" in text, "non-URL text is kept"
    assert "<url>" in text
    _assert_no_leak(text)


@pytest.mark.parametrize(("status", "reason"), [(401, "authError"), (403, "accessNotConfigured")])
def test_google_free_text_is_withheld_for_401_and_403(status: int, reason: str) -> None:
    """docs/security.md: for 401 and 403 only status, reason, and a fixed hint remain."""
    _require_redactor()
    google = (
        FakeGoogle()
        .on("GET", TASKLISTS, _google_error(status, reason, LEAKY_MESSAGE), status=status)
        # A 401 makes google-auth refresh and retry once; let the refresh
        # succeed so the final 401 reaches the error mapper.
        .on("POST", r"^/token$", {"access_token": "ya29.e2e-refreshed", "expires_in": 3599})
    )
    result = _call_tasks(google)
    text = result_text(result)
    assert result.is_error
    assert text.startswith(f"Google API error {status} ({reason}).")
    assert "has not been used in project 123" not in text
    _assert_no_leak(text)
    # Exactly status, reason, and the fixed hint: nothing from Google's body.
    from googlemcp import context

    withheld = getattr(context, "withheld_message", None)
    assert withheld is not None, "core must expose the fixed 401/403 text"
    assert text == withheld(status, reason)


def test_401_triggers_one_refresh_then_retries_with_the_new_token() -> None:
    new_token = "ya29.e2e-rotated"
    google = (
        FakeGoogle()
        .on("GET", TASKLISTS, _google_error(401, "authError", "Invalid Credentials"), 401, 1)
        .on("POST", r"^/token$", {"access_token": new_token, "expires_in": 3599})
        .on("GET", TASKLISTS, {"items": [{"id": "l1", "title": "List"}]})
    )
    result = _call_tasks(google)
    assert not result.is_error, result_text(result)
    assert result.structured_content["tasklists"] == [{"id": "l1", "title": "List"}]
    first, refresh, retry = google.requests
    assert first.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert refresh.host == "oauth2.googleapis.com"
    assert retry.headers["authorization"] == f"Bearer {new_token}"
    assert_no_secrets([result_text(result)])


def test_401_with_revoked_refresh_token_is_actionable_and_secret_free() -> None:
    google = (
        FakeGoogle()
        .on("GET", TASKLISTS, _google_error(401, "authError", "Invalid Credentials"), 401)
        .on(
            "POST",
            r"^/token$",
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            400,
        )
    )
    result = _call_tasks(google)
    text = result_text(result)
    assert result.is_error
    assert "invalid_grant" in text
    assert "googlemcp auth login" in text
    assert_no_secrets([text])


def test_server_errors_are_not_retried_into_success_and_stay_clean() -> None:
    google = FakeGoogle().on("GET", TASKLISTS, _google_error(503, "backendError", "x"), status=503)
    result = _call_tasks(google)
    assert result.is_error
    assert "Google API error 503" in result_text(result)
    assert len(google.requests) == 1  # max_retries=0 in the harness


def test_malformed_response_is_masked_and_logged_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    google = FakeGoogle().on("GET", TASKLISTS, b"<html>not json</html>")
    with caplog.at_level(logging.DEBUG):
        result = _call_tasks(google)
    text = result_text(result)
    assert result.is_error
    assert "internal error" in text
    assert "<html>" not in text
    assert_no_secrets([text, caplog.text])


class _TokenEndpoint:
    """google-auth transport that answers the token endpoint with invalid_grant."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, method: str = "GET", body: Any = None, **_: Any) -> Any:
        self.calls += 1
        assert url == TOKEN_URI

        class _Resp:
            status = 400
            headers: dict[str, str] = {}  # noqa: RUF012
            data = json.dumps(
                {
                    "error": "invalid_grant",
                    "error_description": "Token has been expired or revoked.",
                }
            ).encode()

        return _Resp()


def test_refresh_failure_is_actionable_and_secret_free(caplog: pytest.LogCaptureFixture) -> None:
    endpoint = _TokenEndpoint()
    store = account_store()
    expired = Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri=TOKEN_URI,
    )
    provider = CredentialProvider(store, request_factory=lambda: endpoint, credentials=expired)
    google = FakeGoogle()
    with caplog.at_level(logging.DEBUG):
        result = _call_tasks(google, provider=provider, store=store)
    text = result_text(result)
    assert result.is_error
    assert "invalid_grant" in text
    assert "googlemcp auth login" in text
    assert endpoint.calls >= 1
    assert google.requests == [], "no API call without a valid token"
    assert_no_secrets([text, caplog.text])


def test_rest_adapter_errors_are_tool_errors() -> None:
    require_adapter("photos")
    google = FakeGoogle().on(
        "GET",
        r"^/v1/albums$",
        {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "echo"}},
        status=403,
    )
    result = call_tool(
        make_server(load_settings(products=["photos"]), google), "photos_list_app_albums"
    )
    text = result_text(result)
    assert result.is_error
    assert "Google API error 403 (PERMISSION_DENIED)" in text
    assert_no_secrets([text])


def test_validation_errors_reach_the_client_without_a_google_call() -> None:
    require_adapter("youtube_analytics")
    google = FakeGoogle()
    mcp = make_server(load_settings(products=["youtube_analytics"]), google)
    result = call_tool(
        mcp,
        "youtube_analytics_query_report",
        {"metrics": ["views"], "start_date": "2026-09-30", "end_date": "2026-09-01"},
    )
    assert result.is_error
    assert "start_date must be on or before end_date" in result_text(result)
    assert google.requests == []
