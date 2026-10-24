"""End-to-end harness: the real server, real credentials plumbing, fake Google.

What is real: ``build_server`` with every discovered adapter, policy
selection, FastMCP tool wrapping, ``GoogleServiceContext`` (client building,
retries, error mapping), ``CredentialProvider``, and google-auth header
injection. What is fake: the network. Discovery-based calls go through
``RouterHttp`` (an ``httplib2.Http``), REST calls through ``RouterAdapter``
(a ``requests`` transport adapter). Both answer from a route table and
record every request, so tests can assert what reached "Google".
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httplib2
import pytest
import requests
from fastmcp import Client, FastMCP
from google.oauth2.credentials import Credentials
from requests.adapters import BaseAdapter

from conftest import MemoryStore
from googlemcp.auth.credentials import TOKEN_URI, CredentialProvider
from googlemcp.catalog import load_catalog
from googlemcp.config import Settings
from googlemcp.context import GoogleServiceContext
from googlemcp.secretstore import SecretKey, SecretStore
from googlemcp.server import build_server
from googlemcp.services import discover_adapters
from googlemcp.services.base import ServiceAdapter

# Distinctive fake secrets: tests assert none of them ever reaches a client.
# They are deliberately shorter than real tokens so .gitleaks.toml stays quiet.
ACCESS_TOKEN = "ya29.e2e-fake-access"
REFRESH_TOKEN = "1//0e2e-fake-refresh-token"
CLIENT_ID = "e2e-client.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-e2e-fake-client-secret-000"
SECRETS = (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET)


@dataclass
class Recorded:
    method: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    status: int
    body: Any
    times: int | None = None
    hits: int = 0

    def matches(self, rec: Recorded) -> bool:
        if self.times is not None and self.hits >= self.times:
            return False
        return self.method == rec.method and bool(self.pattern.search(rec.path))


@dataclass
class FakeGoogle:
    """Route table plus request log shared by both transports."""

    routes: list[Route] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)
    unmatched: list[Recorded] = field(default_factory=list)

    def on(
        self, method: str, path_regex: str, body: Any, status: int = 200, times: int | None = None
    ) -> FakeGoogle:
        """Answer matching requests; ``times`` limits how often (first match wins)."""
        self.routes.append(Route(method.upper(), re.compile(path_regex), status, body, times))
        return self

    def answer(self, rec: Recorded) -> tuple[int, bytes]:
        self.requests.append(rec)
        for route in self.routes:
            if route.matches(rec):
                route.hits += 1
                body = route.body
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return route.status, raw
        self.unmatched.append(rec)
        return 404, json.dumps({"error": {"code": 404, "status": "NOT_FOUND"}}).encode()


def _lower(headers: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}


def _decode(body: Any) -> Any:
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return body
    return body


class RouterHttp(httplib2.Http):
    """httplib2 stand-in used by googleapiclient through AuthorizedHttp."""

    def __init__(self, google: FakeGoogle):
        super().__init__(timeout=5)
        self._google = google

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        redirections: int = 5,
        connection_type: Any = None,
    ) -> tuple[httplib2.Response, bytes]:
        status, raw = self._google.answer(
            Recorded(method.upper(), uri, _lower(headers), _decode(body))
        )
        resp = httplib2.Response({"status": str(status), "content-type": "application/json"})
        return resp, raw


class RouterAdapter(BaseAdapter):
    """requests transport adapter used under google-auth's AuthorizedSession."""

    def __init__(self, google: FakeGoogle):
        super().__init__()
        self._google = google

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        rec = Recorded(
            str(request.method).upper(),
            str(request.url),
            _lower(request.headers),
            _decode(request.body),
        )
        status, raw = self._google.answer(rec)
        resp = requests.Response()
        resp.status_code = status
        resp._content = raw
        resp.headers["Content-Type"] = "application/json"
        resp.url = str(request.url)
        resp.request = request
        return resp

    def close(self) -> None:
        return None


class RoutedContext(GoogleServiceContext):
    """The production context with its transports pointed at ``FakeGoogle``."""

    def __init__(self, provider: CredentialProvider, google: FakeGoogle):
        super().__init__(
            provider,
            max_retries=0,
            sleep=lambda _seconds: None,
            http_factory=lambda: RouterHttp(google),
        )
        self._google = google

    def authorized_session(self) -> Any:
        session = super().authorized_session()
        session.mount("https://", RouterAdapter(self._google))
        session.mount("http://", RouterAdapter(self._google))
        return session


def account_store() -> MemoryStore:
    values = {
        SecretKey.OAUTH_CLIENT_ID: CLIENT_ID,
        SecretKey.OAUTH_CLIENT_SECRET: CLIENT_SECRET,
        SecretKey.REFRESH_TOKEN: REFRESH_TOKEN,
    }
    return MemoryStore(values)


def valid_credentials() -> Credentials:
    """Credentials with a live access token, so no refresh call happens."""
    return Credentials(  # type: ignore[no-untyped-call]
        token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri=TOKEN_URI,
    )


# Set to 1 on integrated branches (CI sets it on main) so a missing adapter
# fails instead of skipping. Feature branches carry only some adapters.
STRICT_ENV = "E2E_REQUIRE_ALL_ADAPTERS"


def all_adapters() -> dict[str, ServiceAdapter]:
    return discover_adapters()


def strict_mode() -> bool:
    return os.environ.get(STRICT_ENV) == "1"


def missing_adapters() -> list[str]:
    """Catalog products with no adapter on this branch."""
    return sorted(set(load_catalog()) - set(all_adapters()))


def assert_adapters_complete() -> None:
    """Strict mode: every catalog product has an adapter. Otherwise skip."""
    missing = missing_adapters()
    if not missing:
        return
    message = f"catalog products without an adapter: {', '.join(missing)}"
    if strict_mode():
        pytest.fail(f"{message} ({STRICT_ENV}=1)")
    pytest.skip(message)


def require_adapter(product: str) -> None:
    """Skip (or fail in strict mode) when ``product`` has no adapter here."""
    if product in all_adapters():
        return
    message = f"{product} adapter is not on this branch"
    if strict_mode():
        pytest.fail(f"{message} ({STRICT_ENV}=1)")
    pytest.skip(message)


def make_server(
    settings: Settings,
    google: FakeGoogle,
    *,
    provider: CredentialProvider | None = None,
    store: SecretStore | None = None,
) -> FastMCP:
    store = store or account_store()
    provider = provider or CredentialProvider(store, credentials=valid_credentials())
    return build_server(
        settings,
        store,
        adapters=all_adapters(),
        ctx=RoutedContext(provider, google),
    )


def list_tools(mcp: FastMCP) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(run())


def call_tool(mcp: FastMCP, name: str, args: dict[str, Any] | None = None) -> Any:
    """Call through the MCP client without raising, so tests see ``is_error``."""

    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(run())


def result_text(result: Any) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


def assert_no_secrets(texts: Iterable[str]) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, "a credential leaked into client-visible output"
