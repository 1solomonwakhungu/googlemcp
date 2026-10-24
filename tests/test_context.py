import json
from typing import Any

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMock, HttpMockSequence

from googlemcp.auth import AuthError
from googlemcp.context import GoogleServiceContext
from googlemcp.services.base import UpstreamError


class StaticProvider:
    def __init__(self) -> None:
        self.creds = Credentials(token="ya29.static")  # type: ignore[no-untyped-call]

    def get(self) -> Credentials:
        return self.creds


class FakeRequest:
    """Stands in for googleapiclient.http.HttpRequest."""

    def __init__(self, method: str, outcomes: list[Any]):
        self.method = method
        self.uri = "https://tasks.googleapis.com/tasks/v1/lists?secretQuery=private"
        self.outcomes = outcomes
        self.calls = 0

    def execute(self) -> Any:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(
    status: int, reason: str | None = None, message: str = "boom", **headers: str
) -> HttpError:
    error: dict[str, Any] = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"reason": reason, "message": message}]
    resp = httplib2.Response({"status": str(status), **headers})
    return HttpError(resp, json.dumps({"error": error}).encode(), uri="https://x/?q=private")


def _ctx(sleeps: list[float], retries: int = 4) -> GoogleServiceContext:
    return GoogleServiceContext(
        StaticProvider(),  # type: ignore[arg-type]
        max_retries=retries,
        sleep=sleeps.append,
    )


def test_returns_dict_results() -> None:
    assert _ctx([]).execute(FakeRequest("GET", [{"ok": True}])) == {"ok": True}


def test_media_bytes_pass_through_unchanged() -> None:
    sleeps: list[float] = []
    data = b"%PDF-1.7 binary \x00\xff"
    request = FakeRequest("GET", [http_error(503), data])
    assert _ctx(sleeps).execute(request) == data
    assert len(sleeps) == 1


def test_media_bytes_from_real_googleapiclient_request() -> None:
    ctx = _ctx([])
    from googleapiclient.discovery import build

    service = build(
        "drive",
        "v3",
        http=HttpMock(headers={"status": "200"}),
        static_discovery=True,
        cache_discovery=False,
    )
    request = service.files().export(fileId="abc", mimeType="text/plain")
    request.http = HttpMockSequence([({"status": "200"}, b"plain text body")])
    assert ctx.execute(request) == b"plain text body"


def test_retries_429_for_any_method_then_succeeds() -> None:
    sleeps: list[float] = []
    request = FakeRequest("POST", [http_error(429), http_error(429), {"id": "1"}])
    assert _ctx(sleeps).execute(request) == {"id": "1"}
    assert request.calls == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] / 2


def test_rate_limit_403_is_retried() -> None:
    request = FakeRequest("GET", [http_error(403, "userRateLimitExceeded"), {"ok": 1}])
    assert _ctx([]).execute(request) == {"ok": 1}


def test_retry_after_header_is_honored() -> None:
    sleeps: list[float] = []
    request = FakeRequest("GET", [http_error(429, **{"retry-after": "20"}), {}])
    _ctx(sleeps).execute(request)
    assert sleeps[0] >= 20


def test_5xx_retried_for_get_but_not_post() -> None:
    get = FakeRequest("GET", [http_error(503), http_error(500), {"ok": 1}])
    assert _ctx([]).execute(get) == {"ok": 1}
    post = FakeRequest("POST", [http_error(503), {"sent": 1}])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(post)
    assert info.value.status == 503
    assert post.calls == 1


def test_retries_are_bounded() -> None:
    sleeps: list[float] = []
    request = FakeRequest("GET", [http_error(503)] * 10)
    with pytest.raises(UpstreamError):
        _ctx(sleeps, retries=2).execute(request)
    assert request.calls == 3
    assert len(sleeps) == 2
    assert all(s <= 32 for s in sleeps)


def test_errors_are_sanitized() -> None:
    request = FakeRequest("GET", [http_error(404, "notFound", "Requested entity was not found.")])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    text = str(info.value)
    assert text == "Google API error 404 (notFound): Requested entity was not found."
    assert "private" not in text and "googleapis.com" not in text
    assert info.value.reason == "notFound"


def test_long_messages_are_truncated() -> None:
    request = FakeRequest("GET", [http_error(400, "badRequest", "x" * 5000)])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    assert len(str(info.value)) < 400


def test_insufficient_scope_gets_login_hint() -> None:
    request = FakeRequest("GET", [http_error(403, "insufficientPermissions")])
    with pytest.raises(UpstreamError, match="auth login"):
        _ctx([]).execute(request)


def test_401_maps_to_auth_error() -> None:
    with pytest.raises(AuthError, match="auth login"):
        _ctx([]).execute(FakeRequest("GET", [http_error(401)]))


def test_refresh_error_during_execute_maps_to_invalid_grant_hint() -> None:
    failure = RefreshError(
        "invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}
    )
    with pytest.raises(AuthError, match="7 days"):
        _ctx([]).execute(FakeRequest("GET", [failure]))


def test_network_errors_retry_only_idempotent() -> None:
    get = FakeRequest("GET", [TimeoutError(), {"ok": 1}])
    assert _ctx([]).execute(get) == {"ok": 1}
    post = FakeRequest("POST", [ConnectionResetError()])
    with pytest.raises(UpstreamError, match="network error"):
        _ctx([]).execute(post)


def test_client_builds_from_bundled_discovery_with_fresh_http() -> None:
    ctx = _ctx([])
    first = ctx.client("tasks", "v1")
    second = ctx.client("tasks", "v1")
    request = first.tasklists().list(maxResults=5)
    assert request.uri.startswith("https://tasks.googleapis.com/tasks/v1/users/@me/lists")
    assert first._http is not second._http


def test_unknown_api_is_an_upstream_error() -> None:
    with pytest.raises(UpstreamError, match="no bundled discovery document"):
        _ctx([]).client("notanapi", "v0")


def test_errorinfo_reason_preferred_and_cause_kept() -> None:
    body = {
        "error": {
            "code": 403,
            "message": "Google Tasks API has not been used in project 1 before or it is disabled.",
            "status": "PERMISSION_DENIED",
            "errors": [{"reason": "accessNotConfigured"}],
            "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}
            ],
        }
    }
    resp = httplib2.Response({"status": "403"})
    error = HttpError(resp, json.dumps(body).encode(), uri="https://x/")
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(FakeRequest("GET", [error]))
    assert info.value.reason == "SERVICE_DISABLED"
    assert info.value.status == 403
    assert info.value.__cause__ is error


def test_errors_reason_used_without_errorinfo() -> None:
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(FakeRequest("GET", [http_error(400, "invalid")]))
    assert info.value.reason == "invalid"


def test_url_and_token_in_error_message_are_redacted() -> None:
    message = "Access denied for https://example.invalid/private?token=synthetic resource."
    request = FakeRequest("GET", [http_error(400, "badRequest", message)])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    text = str(info.value)
    assert "example.invalid" not in text and "token=" not in text and "synthetic" not in text
    assert text == "Google API error 400 (badRequest): Access denied for <url> resource."
    assert info.value.status == 400 and info.value.reason == "badRequest"


def test_redaction_happens_before_the_length_cap() -> None:
    message = "x" * 280 + " https://example.invalid/" + "p" * 100 + "?token=synthetic"
    request = FakeRequest("GET", [http_error(400, "badRequest", message)])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    text = str(info.value)
    assert "example.invalid" not in text and "https://" not in text and "token" not in text


def test_normal_upstream_message_is_unchanged() -> None:
    request = FakeRequest("GET", [http_error(404, "notFound", "Requested entity was not found.")])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    assert str(info.value) == "Google API error 404 (notFound): Requested entity was not found."


def test_percent_encoded_url_in_google_message_is_redacted() -> None:
    message = "Denied for https%3A%2F%2Fexample.invalid%2Fprivate%3Ftoken%3Dsynthetic"
    request = FakeRequest("GET", [http_error(400, "badRequest", message)])
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(request)
    text = str(info.value)
    assert text == "Google API error 400 (badRequest): Denied for <url>"
    assert "synthetic" not in text and "%3A" not in text


QUOTED_LEAK = "Denied https://example.invalid/private?access_token='synthetic'"


@pytest.mark.parametrize(
    ("status", "reason", "hint"),
    [
        (403, "forbidden", "Google denied the request; details withheld."),
        (403, "SERVICE_DISABLED", "is not enabled for the OAuth client's project"),
        (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "lacks a scope this tool needs"),
        (401, "UNAUTHENTICATED", "rejected the credentials; run `googlemcp auth login`"),
    ],
)
def test_401_and_403_withhold_google_free_text(status: int, reason: str, hint: str) -> None:
    error = http_error(status, reason, QUOTED_LEAK + ' {"access_token": "synthetic"}')
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(FakeRequest("GET", [error]))
    text = str(info.value)
    assert text.startswith(f"Google API error {status} ({reason}).")
    assert hint in text
    for leak in ("Denied", "example.invalid", "synthetic", "access_token"):
        assert leak not in text
    assert info.value.status == status and info.value.reason == reason
    assert isinstance(info.value, AuthError) is (status == 401)
    # The raw response stays available internally on the chained HttpError.
    assert info.value.__cause__ is error
    assert b"synthetic" in error.content


def test_quoted_value_after_url_is_redacted_in_other_errors() -> None:
    error = http_error(400, "badRequest", QUOTED_LEAK)
    with pytest.raises(UpstreamError) as info:
        _ctx([]).execute(FakeRequest("GET", [error]))
    assert str(info.value) == "Google API error 400 (badRequest): Denied <url>"
