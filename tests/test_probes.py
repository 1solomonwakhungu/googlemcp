import json
import sys
import types
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from google.auth.exceptions import RefreshError

from googlemcp.catalog import load_catalog
from googlemcp.probes import (
    MISSING_ID,
    PROBES,
    STATUSES,
    ProbeResult,
    classify,
    run_probes,
)
from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.testing import FakeContext

# Drive Activity has no GET endpoint; activity.query is a read-only POST.
POST_ALLOWED = {"driveactivity"}


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b"{}"


@dataclass
class FakeSession:
    responses: list[FakeResponse]
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


@dataclass
class ProbeContext(FakeContext):
    """FakeContext plus an authorized_session fake for REST-only probes."""

    session: FakeSession = field(default_factory=lambda: FakeSession([FakeResponse(200)]))

    def authorized_session(self) -> Any:
        return self.session


def _error_body(
    code: int,
    status: str,
    reason: str | None = None,
    legacy: str | None = None,
    message: str = "secret-ish text 123456",
) -> str:
    error: dict[str, Any] = {"code": code, "message": message, "status": status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    if legacy:
        error["errors"] = [{"reason": legacy}]
    return json.dumps({"error": error})


def _ok_ctx() -> ProbeContext:
    return (
        ProbeContext().respond({"messagesTotal": 3, "items": [{"id": "c"}]}).respond({}).respond({})
    )


def test_result_has_exactly_the_agreed_fields() -> None:
    assert [f.name for f in fields(ProbeResult)] == ["product", "ok", "status", "detail"]
    assert ProbeResult("photos", False, "skipped", "x").skipped
    assert not ProbeResult("photos", True, "ok", "x").skipped


def test_every_catalog_product_has_a_probe() -> None:
    assert set(PROBES) == set(load_catalog())


@pytest.mark.parametrize("product", sorted(PROBES))
def test_probe_succeeds_with_read_only_requests(product: str) -> None:
    ctx = _ok_ctx()
    result = PROBES[product](ctx)

    assert result == ProbeResult(product, True, "ok", result.detail), result
    methods = {r.method for r in ctx.requests} | {c[0] for c in ctx.session.calls}
    assert methods, "probe made no request"
    allowed = {"GET", "POST"} if product in POST_ALLOWED else {"GET"}
    assert methods <= allowed, methods
    assert len(ctx.requests) + len(ctx.session.calls) <= 2


def test_driveactivity_post_is_a_single_item_query() -> None:
    ctx = _ok_ctx()
    PROBES["driveactivity"](ctx)
    (req,) = ctx.requests
    assert req.method == "POST"
    assert req.uri.split("?")[0].endswith("/v2/activity:query")
    assert req.body == {"pageSize": 1}


def test_specific_probe_requests() -> None:
    expectations = {
        "gmail": "/gmail/v1/users/me/profile",
        "drive": "/drive/v3/about?fields=kind",
        "docs": f"/v1/documents/{MISSING_ID}",
        "people": "/v1/people/me?personFields=metadata",
        "youtube": "/youtube/v3/channels?",
        "youtube_reporting": "/v1/reportTypes?pageSize=1",
        "searchconsole": "/webmasters/v3/sites?",
        "appsscript": f"script.googleapis.com/v1/projects/{MISSING_ID}?",
    }
    for product, fragment in expectations.items():
        ctx = _ok_ctx()
        PROBES[product](ctx)
        assert fragment in ctx.requests[0].uri, (product, ctx.requests[0].uri)


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        (
            "youtube",
            {"part": ["id"], "mine": ["true"], "maxResults": ["1"], "fields": ["items(id)"]},
        ),
        ("youtube_analytics", {"mine": ["true"], "fields": ["kind"]}),
        ("searchconsole", {"fields": ["siteEntry(permissionLevel)"]}),
        ("appsscript", {"fields": ["scriptId"]}),
    ],
)
def test_probe_query_limits_response_size(product: str, expected: dict[str, list[str]]) -> None:
    ctx = _ok_ctx()
    PROBES[product](ctx)
    query = parse_qs(urlparse(ctx.requests[0].uri).query)
    query.pop("alt", None)
    assert query == expected


def test_appsscript_probe_404_is_ok() -> None:
    ctx = ProbeContext().respond(_error_body(404, "NOT_FOUND"), 404)
    result = PROBES["appsscript"](ctx)
    assert (result.ok, result.status) == (True, "ok")
    assert ctx.requests[0].method == "GET"


def test_appsscript_user_setting_off_is_not_enabled() -> None:
    message = (
        "User has not enabled the Apps Script API. Enable it by visiting "
        "https://script.google.com/home/usersettings then retry."
    )
    ctx = ProbeContext().respond(_error_body(403, "PERMISSION_DENIED", message=message), 403)
    assert PROBES["appsscript"](ctx) == ProbeResult(
        "appsscript",
        False,
        "not_enabled",
        "Apps Script API user setting is off (script.google.com/home/usersettings)",
    )


def test_appsscript_generic_403_is_permission() -> None:
    ctx = ProbeContext().respond(_error_body(403, "PERMISSION_DENIED"), 403)
    assert PROBES["appsscript"](ctx).status == "permission"


def test_analytics_probes_admin_and_data_apis() -> None:
    ctx = _ok_ctx()
    assert PROBES["analytics"](ctx).ok
    admin, data = ctx.requests
    assert "analyticsadmin.googleapis.com/v1beta/accountSummaries" in admin.uri
    assert "analyticsdata.googleapis.com/v1beta/properties/0/metadata" in data.uri


def test_photos_uses_authorized_session_and_skips_picker() -> None:
    ctx = ProbeContext()
    result = PROBES["photos"](ctx)
    assert result.ok
    assert "Picker API not probed" in result.detail
    ((method, url, kwargs),) = ctx.session.calls
    assert method == "GET"
    assert url == "https://photoslibrary.googleapis.com/v1/albums"
    assert kwargs["params"] == {"pageSize": 1}
    assert kwargs["timeout"] > 0


def test_photos_http_error_is_classified() -> None:
    body = _error_body(403, "PERMISSION_DENIED", reason="SERVICE_DISABLED").encode()
    ctx = ProbeContext(session=FakeSession([FakeResponse(403, body)]))
    assert PROBES["photos"](ctx).status == "not_enabled"


def test_gmail_ok_reports_presence_only() -> None:
    ctx = ProbeContext().respond({"emailAddress": "owner@example.com", "messagesTotal": 42})
    result = PROBES["gmail"](ctx)
    assert result.detail == "profile readable (messagesTotal present)"
    assert "owner@example.com" not in result.detail and "42" not in result.detail


def test_200_ok() -> None:
    assert PROBES["tasks"](ProbeContext().respond({"items": []})).status == "ok"


def test_403_service_disabled_is_not_enabled() -> None:
    ctx = ProbeContext().respond(
        _error_body(403, "PERMISSION_DENIED", reason="SERVICE_DISABLED"), 403
    )
    result = PROBES["calendar"](ctx)
    assert result == ProbeResult(
        "calendar", False, "not_enabled", "API not enabled in the project (SERVICE_DISABLED)"
    )
    assert "123456" not in result.detail


def test_legacy_access_not_configured_is_not_enabled() -> None:
    ctx = ProbeContext().respond(
        _error_body(403, "PERMISSION_DENIED", legacy="accessNotConfigured"), 403
    )
    assert PROBES["youtube"](ctx).status == "not_enabled"


def test_401_is_auth_error() -> None:
    ctx = ProbeContext().respond(_error_body(401, "UNAUTHENTICATED"), 401)
    result = PROBES["gmail"](ctx)
    assert (result.ok, result.status) == (False, "auth_error")


def test_insufficient_scope_is_permission() -> None:
    ctx = ProbeContext().respond(
        _error_body(403, "PERMISSION_DENIED", reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT"), 403
    )
    assert PROBES["appsscript"](ctx).status == "permission"


def test_plain_403_is_permission() -> None:
    ctx = ProbeContext().respond(_error_body(403, "PERMISSION_DENIED"), 403)
    assert PROBES["drive"](ctx).status == "permission"


@pytest.mark.parametrize("product", ["docs", "sheets", "slides", "forms"])
def test_not_found_probes_treat_404_as_ok(product: str) -> None:
    ctx = ProbeContext().respond(_error_body(404, "NOT_FOUND"), 404)
    result = PROBES[product](ctx)
    assert (result.ok, result.status) == (True, "ok")


@pytest.mark.parametrize("product", ["docs", "sheets", "slides", "forms"])
def test_not_found_probes_generic_403_is_permission(product: str) -> None:
    ctx = ProbeContext().respond(_error_body(403, "PERMISSION_DENIED"), 403)
    result = PROBES[product](ctx)
    assert result == ProbeResult(product, False, "permission", "HTTP 403: access denied")


def test_not_found_probe_403_reasons_from_upstream_error() -> None:
    generic = UpstreamError("x", status=403, reason="PERMISSION_DENIED")
    assert classify("sheets", generic, not_found_ok=True).status == "permission"
    scope = UpstreamError("x", status=403, reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    assert classify("sheets", scope, not_found_ok=True).status == "permission"
    legacy = UpstreamError("x", status=403, reason="accessNotConfigured")
    assert classify("forms", legacy, not_found_ok=True).status == "not_enabled"


def test_not_found_probe_400_is_error() -> None:
    ctx = ProbeContext().respond(_error_body(400, "INVALID_ARGUMENT"), 400)
    assert PROBES["slides"](ctx) == ProbeResult("slides", False, "error", "HTTP 400")


def test_not_found_probe_still_reports_disabled_api() -> None:
    ctx = ProbeContext().respond(
        _error_body(403, "PERMISSION_DENIED", reason="SERVICE_DISABLED"), 403
    )
    assert PROBES["sheets"](ctx).status == "not_enabled"


def test_404_on_listing_probe_is_error() -> None:
    ctx = ProbeContext().respond(_error_body(404, "NOT_FOUND"), 404)
    assert PROBES["tasks"](ctx) == ProbeResult("tasks", False, "error", "HTTP 404")


CONSUMER_MESSAGE = "Google Chat API is only available to Google Workspace users."


@pytest.mark.parametrize("product", ["meet", "chat"])
def test_workspace_only_generic_403_is_permission(product: str) -> None:
    ctx = ProbeContext().respond(_error_body(403, "PERMISSION_DENIED"), 403)
    assert PROBES[product](ctx) == ProbeResult(
        product,
        False,
        "permission",
        "permission denied; Meet/Chat REST APIs require a Google Workspace account, "
        "which may be the cause",
    )


@pytest.mark.parametrize("product", ["meet", "chat"])
def test_workspace_only_documented_consumer_message_is_unsupported(product: str) -> None:
    body = _error_body(403, "PERMISSION_DENIED", message=CONSUMER_MESSAGE)
    result = PROBES[product](ProbeContext().respond(body, 403))
    assert (result.ok, result.status) == (False, "unsupported_account")
    assert "only available" not in result.detail


def test_consumer_message_is_read_from_upstream_error_cause() -> None:
    ctx = ProbeContext().respond(
        _error_body(400, "FAILED_PRECONDITION", message=CONSUMER_MESSAGE), 400
    )
    assert PROBES["chat"](ctx).status == "unsupported_account"


def test_chat_app_not_configured_404_is_not_enabled() -> None:
    message = (
        "Google Chat app not found. To create a Chat app, you must turn on the Chat API "
        "and configure the app in the Google Cloud console."
    )
    ctx = ProbeContext().respond(_error_body(404, "NOT_FOUND", message=message), 404)
    result = PROBES["chat"](ctx)
    assert (result.status, result.ok) == ("not_enabled", False)
    assert "Chat API Configuration" in result.detail


@pytest.mark.parametrize("product", ["meet", "chat"])
def test_workspace_only_other_404_is_error(product: str) -> None:
    ctx = ProbeContext().respond(_error_body(404, "NOT_FOUND"), 404)
    assert PROBES[product](ctx) == ProbeResult(product, False, "error", "HTTP 404")


def test_workspace_only_bare_upstream_403_is_permission() -> None:
    result = classify("meet", UpstreamError("x", status=403), workspace_only=True)
    assert result.status == "permission"


def test_workspace_product_still_reports_disabled_api() -> None:
    ctx = ProbeContext().respond(
        _error_body(403, "PERMISSION_DENIED", reason="SERVICE_DISABLED"), 403
    )
    assert PROBES["meet"](ctx).status == "not_enabled"


def test_429_is_error() -> None:
    ctx = ProbeContext().respond(_error_body(429, "RESOURCE_EXHAUSTED"), 429)
    assert "429" in PROBES["youtube"](ctx).detail


def test_not_found_probe_bare_403_without_reasons_is_permission() -> None:
    exc = UpstreamError("Google API error 403", status=403)
    assert classify("docs", exc, not_found_ok=True).status == "permission"
    assert classify("docs", UpstreamError("x", status=404), not_found_ok=True).ok


def test_classify_uses_upstream_reason_without_http_cause() -> None:
    exc = UpstreamError("boom", status=403, reason="SERVICE_DISABLED")
    assert classify("docs", exc, not_found_ok=True).status == "not_enabled"


def test_refresh_errors_are_auth_errors_even_when_wrapped() -> None:
    assert classify("gmail", RefreshError("invalid_grant")).status == "auth_error"
    wrapped = GoogleMCPError("auth failed")
    wrapped.__cause__ = RefreshError("invalid_grant")
    assert classify("gmail", wrapped).status == "auth_error"


def test_core_auth_error_is_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class AuthError(GoogleMCPError):
        pass

    module = types.ModuleType("googlemcp.auth")
    module.AuthError = AuthError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlemcp.auth", module)
    assert classify("gmail", AuthError("HTTP 401")).status == "auth_error"


def test_core_auth_error_subclassing_upstream_wins_over_network_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AuthError(UpstreamError):
        pass

    module = types.ModuleType("googlemcp.auth")
    module.AuthError = AuthError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlemcp.auth", module)
    network = AuthError("refresh failed", status=None, reason="network")
    assert classify("gmail", network).status == "auth_error"
    rejected = AuthError("rejected", status=401, reason="invalid_grant")
    assert classify("docs", rejected, not_found_ok=True).status == "auth_error"


def test_network_failure_without_status() -> None:
    result = classify("gmail", UpstreamError("connection reset", status=None))
    assert result == ProbeResult("gmail", False, "error", "request failed before an HTTP response")


def test_unexpected_exception_reports_type_only() -> None:
    result = classify("gmail", RuntimeError("token=abc123"))
    assert result == ProbeResult("gmail", False, "error", "probe failed: RuntimeError")


class ExplodingContext(ProbeContext):
    def client(self, api: str, version: str) -> Any:
        if api == "gmail":
            raise RefreshError("invalid_grant: token=abc")
        return super().client(api, version)


def test_run_probes_never_raises_and_keeps_order() -> None:
    ctx = ExplodingContext().respond({}).respond({})
    results = run_probes(ctx, ["tasks", "gmail", "nope", "calendar"])
    assert [r.product for r in results] == ["tasks", "gmail", "nope", "calendar"]
    assert [r.status for r in results] == ["ok", "auth_error", "error", "ok"]
    assert results[2].detail == "no probe for product"
    assert all("abc" not in r.detail for r in results)


def test_run_probes_defaults_to_whole_catalog() -> None:
    results = run_probes(ProbeContext())
    assert [r.product for r in results] == list(load_catalog())
    assert all(r.status in STATUSES for r in results)
