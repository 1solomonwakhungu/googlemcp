"""Shared fixtures for core tests. Everything here is offline."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from googlemcp.secretstore import SecretKey
from googlemcp.services.base import JSON, ServiceContext, ServiceSpec, ToolRegistry, UpstreamError


class FakeAdapter:
    """Adapter with one read, one write, and one destructive tool."""

    spec = ServiceSpec(
        key="tasks",
        title="Fake Tasks",
        api_services=("tasks.googleapis.com",),
        read_scopes=("scope.read",),
        write_scopes=("scope.write",),
    )

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("tasks_fake_read", idempotent=True)
        def fake_read(name: str = "world", limit: int = 5) -> JSON:
            """Read something harmless."""
            return {"hello": name, "limit": limit}

        @registry.tool("tasks_fake_write", write=True)
        def fake_write(title: str) -> JSON:
            """Write something."""
            return {"created": title}

        @registry.tool("tasks_fake_delete", write=True, destructive=True)
        def fake_delete(item_id: str) -> JSON:
            """Delete something."""
            return {"deleted": item_id}

        @registry.tool("tasks_fake_upstream_error")
        def fake_upstream_error() -> JSON:
            """Always fails upstream."""
            raise UpstreamError("Google API error 404 (notFound): gone", status=404)

        @registry.tool("tasks_fake_rest_error")
        def fake_rest_error() -> JSON:
            """REST-only adapter copying response text into its own error."""
            raise UpstreamError(
                "Photos error 403: see https://example.invalid/private?token=synthetic and "
                "https%3A%2F%2Fexample.invalid%2Fprivate%3Ftoken%3Dsynthetic",
                status=403,
                reason="PERMISSION_DENIED",
            )

        @registry.tool("tasks_fake_rest_error_500")
        def fake_rest_error_500() -> JSON:
            """REST-only adapter error that is not 401 or 403."""
            raise UpstreamError(
                "Photos error 500: see https://example.invalid/private?access_token='synthetic' "
                'and {"access_token": "synthetic"}',
                status=500,
            )

        @registry.tool("tasks_fake_crash")
        def fake_crash() -> JSON:
            """Always crashes."""
            raise RuntimeError("secret-internal-detail")

        @registry.tool("tasks_fake_session_refresh")
        def fake_session_refresh() -> JSON:
            """AuthorizedSession refresh failure."""
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant: expired", {"error": "invalid_grant"})

        @registry.tool("tasks_fake_session_network")
        def fake_session_network() -> JSON:
            """AuthorizedSession transport failure."""
            import requests

            raise requests.ConnectionError("dns failure for host with ?key=secret")


class OtherAdapter:
    spec = ServiceSpec(
        key="drive",
        title="Fake Drive",
        api_services=("drive.googleapis.com",),
        read_scopes=("drive.read", "scope.read"),
        write_scopes=("drive.write",),
    )

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("drive_fake_list")
        def fake_list() -> JSON:
            """List files."""
            return {"files": []}


class MemoryStore:
    """In-memory SecretStore for tests."""

    name = "memory"

    def __init__(self, values: dict[SecretKey, str] | None = None, read_only: bool = False):
        self.values: dict[SecretKey, str] = dict(values or {})
        self.read_only = read_only
        self.writes: list[SecretKey] = []

    def get(self, key: SecretKey) -> str | None:
        return self.values.get(key)

    def set(self, key: SecretKey, value: str) -> None:
        if self.read_only:
            from googlemcp.secretstore import ensure_writable

            ensure_writable(self, key)
        self.values[key] = value
        self.writes.append(key)

    def delete(self, key: SecretKey) -> None:
        self.values.pop(key, None)


class NullContext:
    def client(self, api: str, version: str) -> Any:
        raise AssertionError("no Google calls in this test")

    def execute(self, request: Any) -> JSON:
        raise AssertionError("no Google calls in this test")

    def authorized_session(self) -> Any:
        raise AssertionError("no Google calls in this test")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Drop GOOGLEMCP_* from the environment and hide any user config file."""
    import os

    for name in list(os.environ):
        if name.startswith("GOOGLEMCP_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("googlemcp.config.DEFAULT_CONFIG_PATH", tmp_path / "absent.toml")
    yield


@pytest.fixture
def adapters() -> dict[str, Any]:
    return {"tasks": FakeAdapter(), "drive": OtherAdapter()}


@pytest.fixture
def account_store() -> MemoryStore:
    return MemoryStore(
        {
            SecretKey.OAUTH_CLIENT_ID: "client-id.apps.googleusercontent.com",
            SecretKey.OAUTH_CLIENT_SECRET: "client-secret-value",
            SecretKey.REFRESH_TOKEN: "refresh-token-value",
        }
    )
