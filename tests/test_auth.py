import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import MemoryStore
from googlemcp.auth import AuthError, CredentialProvider, login, revoke, status
from googlemcp.auth.credentials import invalid_grant_message
from googlemcp.auth.flow import CLIENTS_CONSOLE_URL, read_client_secrets
from googlemcp.redaction import redact
from googlemcp.secretstore import SecretKey, SecretStoreError
from googlemcp.services.base import UpstreamError

# Fake transport for google-auth refreshes


class FakeTokenEndpoint:
    """Callable matching google.auth.transport.Request; records refresh requests."""

    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None):
        self.status = status
        self.payload = payload or {"access_token": "ya29.fresh", "expires_in": 3599}
        self.bodies: list[str] = []

    def __call__(self, url: str, method: str = "GET", body: Any = None, **_: Any) -> Any:
        self.bodies.append(body.decode() if isinstance(body, bytes) else str(body))
        return SimpleNamespace(
            status=self.status, data=json.dumps(self.payload).encode(), headers={}
        )


def _provider(store: MemoryStore, endpoint: FakeTokenEndpoint) -> CredentialProvider:
    return CredentialProvider(store, request_factory=lambda: endpoint)


def test_missing_secrets_name_every_key() -> None:
    store = MemoryStore({SecretKey.OAUTH_CLIENT_ID: "id"})
    with pytest.raises(AuthError) as info:
        CredentialProvider(store).get()
    message = str(info.value)
    assert info.value.reason == "missing_credentials"
    assert "oauth-client-secret" in message and "refresh-token" in message
    assert "googlemcp auth login" in message


def test_first_run_without_client_says_where_to_get_one() -> None:
    with pytest.raises(AuthError) as info:
        CredentialProvider(MemoryStore()).get()
    message = str(info.value)
    assert "googlemcp auth login --client-secrets" in message
    assert "docs/manual-steps.md" in message


def test_missing_refresh_token_with_stored_client_needs_plain_login(
    account_store: MemoryStore,
) -> None:
    del account_store.values[SecretKey.REFRESH_TOKEN]
    with pytest.raises(AuthError) as info:
        CredentialProvider(account_store).get()
    message = str(info.value)
    assert "Run `googlemcp auth login` to authorize" in message
    assert "--client-secrets" not in message


def test_refresh_uses_stored_token_without_scope_param(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint()
    creds = _provider(account_store, endpoint).get()
    assert creds.token == "ya29.fresh"
    assert creds.valid
    assert "refresh_token=refresh-token-value" in endpoint.bodies[0]
    assert "scope=" not in endpoint.bodies[0]


def test_valid_credentials_are_not_refreshed_again(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint()
    provider = _provider(account_store, endpoint)
    provider.get()
    provider.get()
    assert len(endpoint.bodies) == 1


def test_invalid_grant_maps_to_actionable_error(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(
        400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    )
    with pytest.raises(AuthError) as info:
        _provider(account_store, endpoint).get()
    message = str(info.value)
    assert isinstance(info.value, UpstreamError)
    assert info.value.status == 401 and info.value.reason == "invalid_grant"
    assert "invalid_grant" in message
    assert "googlemcp auth login" in message
    assert "7 days" in message and "Testing" in message
    assert "refresh-token-value" not in message


def test_invalid_client_maps_to_client_hint(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(401, {"error": "invalid_client"})
    with pytest.raises(AuthError, match="--client-secrets"):
        _provider(account_store, endpoint).get()


def test_unknown_refresh_error_code_is_redacted(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(400, {"error": "see https://example.invalid/x?token=synthetic"})
    with pytest.raises(AuthError) as info:
        _provider(account_store, endpoint).get()
    assert "example.invalid" not in str(info.value) and "synthetic" not in str(info.value)
    assert info.value.reason == "see <url>"


def test_rotated_refresh_token_is_persisted(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(
        payload={"access_token": "ya29.x", "expires_in": 3599, "refresh_token": "rotated"}
    )
    _provider(account_store, endpoint).get()
    assert account_store.values[SecretKey.REFRESH_TOKEN] == "rotated"


def test_rotation_on_read_only_store_stays_in_memory(account_store: MemoryStore) -> None:
    account_store.read_only = True
    endpoint = FakeTokenEndpoint(
        payload={"access_token": "ya29.x", "expires_in": 3599, "refresh_token": "rotated"}
    )
    creds = _provider(account_store, endpoint).get()
    assert creds.refresh_token == "rotated"
    assert account_store.values[SecretKey.REFRESH_TOKEN] == "refresh-token-value"


def test_invalid_grant_reloads_a_newer_stored_token_once(account_store: MemoryStore) -> None:
    class Endpoint(FakeTokenEndpoint):
        def __call__(self, url: str, method: str = "GET", body: Any = None, **kw: Any) -> Any:
            text = body.decode() if isinstance(body, bytes) else str(body)
            if "refresh_token=refresh-token-value" in text:
                self.status, self.payload = 400, {"error": "invalid_grant"}
            else:
                self.status, self.payload = 200, {"access_token": "ya29.new", "expires_in": 3599}
            return super().__call__(url, method, body, **kw)

    endpoint = Endpoint()
    provider = _provider(account_store, endpoint)
    provider._load()  # process started with the old token
    account_store.values[SecretKey.REFRESH_TOKEN] = "reissued-token"
    creds = provider.get()
    assert creds.token == "ya29.new"
    assert creds.refresh_token == "reissued-token"
    assert len(endpoint.bodies) == 2


def test_invalid_grant_without_newer_token_is_not_retried(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(400, {"error": "invalid_grant"})
    with pytest.raises(AuthError):
        _provider(account_store, endpoint).get()
    assert len(endpoint.bodies) == 1


# Login


class FakeFlow:
    def __init__(self, refresh_token: str | None, granted: list[str] | None):
        self.refresh_token = refresh_token
        self.granted = granted
        self.kwargs: dict[str, Any] = {}

    def run_local_server(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(refresh_token=self.refresh_token, granted_scopes=self.granted)


def _client_json(tmp_path: Path, kind: str = "installed") -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({kind: {"client_id": "cid", "client_secret": "csecret"}}))
    return path


def test_login_stores_client_and_refresh_token(tmp_path: Path) -> None:
    store = MemoryStore()
    flow = FakeFlow("new-refresh", ["a", "b"])
    seen: dict[str, Any] = {}

    def factory(config: dict[str, Any], scopes: Any) -> FakeFlow:
        seen.update(config=config, scopes=list(scopes))
        return flow

    result = login(
        store,
        ["a", "b"],
        client_secrets=_client_json(tmp_path),
        open_browser=False,
        login_hint="user@example.com",
        flow_factory=factory,
    )
    assert store.values[SecretKey.OAUTH_CLIENT_ID] == "cid"
    assert store.values[SecretKey.OAUTH_CLIENT_SECRET] == "csecret"
    assert store.values[SecretKey.REFRESH_TOKEN] == "new-refresh"
    assert result.granted_scopes == ["a", "b"] and result.missing_scopes == []
    assert seen["config"]["installed"]["client_id"] == "cid"
    assert flow.kwargs["access_type"] == "offline"
    assert flow.kwargs["prompt"] == "consent"
    assert flow.kwargs["include_granted_scopes"] == "true"
    assert flow.kwargs["host"] == "127.0.0.1"
    assert flow.kwargs["login_hint"] == "user@example.com"


def test_login_reports_scopes_the_user_unticked(account_store: MemoryStore) -> None:
    flow = FakeFlow("r", ["a"])
    result = login(account_store, ["a", "b"], flow_factory=lambda c, s: flow)
    assert result.missing_scopes == ["b"]


def test_login_without_reported_grant_claims_nothing(account_store: MemoryStore) -> None:
    result = login(account_store, ["a", "b"], flow_factory=lambda c, s: FakeFlow("r", None))
    assert result.granted_scopes is None and result.missing_scopes is None
    assert account_store.values[SecretKey.REFRESH_TOKEN] == "r"


def test_login_without_refresh_token_fails(account_store: MemoryStore) -> None:
    with pytest.raises(AuthError, match="no refresh token"):
        login(account_store, ["a"], flow_factory=lambda c, s: FakeFlow(None, ["a"]))


def test_login_needs_a_client() -> None:
    with pytest.raises(AuthError) as info:
        login(MemoryStore(), ["a"], flow_factory=lambda c, s: FakeFlow("r", ["a"]))
    message = str(info.value)
    assert "googlemcp auth login --client-secrets" in message
    assert CLIENTS_CONSOLE_URL in message and "Desktop app" in message


def test_login_without_scopes_says_how_to_enable_products() -> None:
    with pytest.raises(AuthError, match="GOOGLEMCP_PRODUCTS"):
        login(MemoryStore(), [], flow_factory=lambda c, s: FakeFlow("r", []))


def test_missing_client_file_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(AuthError) as info:
        read_client_secrets(tmp_path / "nope.json")
    message = str(info.value)
    assert "does not exist" in message and CLIENTS_CONSOLE_URL in message


@pytest.mark.parametrize("content", ["not json", "[]", "{}"])
def test_malformed_client_file_is_actionable(tmp_path: Path, content: str) -> None:
    path = tmp_path / "client.json"
    path.write_text(content)
    with pytest.raises(AuthError) as info:
        read_client_secrets(path)
    assert CLIENTS_CONSOLE_URL in str(info.value)


def test_login_refuses_read_only_store() -> None:
    with pytest.raises(SecretStoreError, match="read-only"):
        login(MemoryStore(read_only=True), ["a"])


def test_web_client_json_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="Desktop app client") as info:
        read_client_secrets(_client_json(tmp_path, kind="web"))
    assert "'web'" in str(info.value) and CLIENTS_CONSOLE_URL in str(info.value)


# Status and revoke


class FakeSession:
    def __init__(self, status: int, payload: dict[str, Any] | None = None):
        self.status = status
        self.payload = payload or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, data: dict[str, Any], **_: Any) -> Any:
        self.calls.append((url, data))
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def test_status_reports_scopes_without_secrets(account_store: MemoryStore) -> None:
    session = FakeSession(200, {"scope": "a c"})
    report = status(
        account_store,
        ["a", "b"],
        session=session,
        provider=_provider(account_store, FakeTokenEndpoint()),
    )
    assert report.refresh_ok
    assert report.scopes_verified
    assert report.granted_scopes == ["a", "c"]
    assert report.missing_scopes == ["b"]
    assert session.calls[0][0] == "https://oauth2.googleapis.com/tokeninfo"
    dumped = json.dumps(report.as_dict())
    assert "refresh-token-value" not in dumped and "ya29" not in dumped


@pytest.mark.parametrize("session", [FakeSession(503), FakeSession(200, {})])
def test_status_with_tokeninfo_failure_is_unverified_not_healthy(
    account_store: MemoryStore, session: FakeSession
) -> None:
    report = status(
        account_store,
        ["a"],
        session=session,
        provider=_provider(account_store, FakeTokenEndpoint()),
    )
    assert report.refresh_ok is True
    assert report.scopes_verified is False
    assert report.missing_scopes is None
    assert report.granted_scopes == []
    assert report.error and "could not be verified" in report.error


def test_status_with_tokeninfo_network_error_is_unverified(account_store: MemoryStore) -> None:
    import requests

    class Down:
        def post(self, *args: Any, **kwargs: Any) -> Any:
            raise requests.ConnectionError("down")

    report = status(
        account_store, ["a"], session=Down(), provider=_provider(account_store, FakeTokenEndpoint())
    )
    assert report.scopes_verified is False and report.missing_scopes is None


def test_status_surfaces_invalid_grant(account_store: MemoryStore) -> None:
    endpoint = FakeTokenEndpoint(400, {"error": "invalid_grant"})
    report = status(account_store, ["a"], provider=_provider(account_store, endpoint))
    assert report.refresh_ok is False
    assert report.error and "7 days" in report.error


def test_status_without_check_makes_no_calls() -> None:
    report = status(MemoryStore(), ["a"], check=False)
    assert not report.client_configured and not report.refresh_token_present
    assert report.refresh_ok is None
    assert report.missing_scopes is None and not report.scopes_verified


@pytest.mark.parametrize(("code", "confirmed"), [(200, True), (400, False)])
def test_revoke_deletes_locally(account_store: MemoryStore, code: int, confirmed: bool) -> None:
    session = FakeSession(code)
    assert revoke(account_store, session=session) is confirmed
    assert SecretKey.REFRESH_TOKEN not in account_store.values
    assert session.calls[0] == (
        "https://oauth2.googleapis.com/revoke",
        {"token": "refresh-token-value"},
    )


def test_revoke_keeps_token_when_google_errors(account_store: MemoryStore) -> None:
    with pytest.raises(AuthError, match="HTTP 503"):
        revoke(account_store, session=FakeSession(503))
    assert account_store.values[SecretKey.REFRESH_TOKEN] == "refresh-token-value"


def test_doc_pointers_survive_tool_error_redaction() -> None:
    # MCP tool errors pass through redact(), which strips URLs; the doc pointers
    # must stay readable there.
    assert "docs/access-longevity.md" in redact(invalid_grant_message())
    with pytest.raises(AuthError) as excinfo:
        CredentialProvider(MemoryStore()).get()
    assert "docs/manual-steps.md" in redact(str(excinfo.value))
