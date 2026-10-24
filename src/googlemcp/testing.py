"""Offline test helpers for adapters.

``FakeContext`` builds real googleapiclient Resources from the static
discovery documents bundled with the library, backed by ``HttpMockSequence``.
Tests therefore exercise real request construction (URLs, query params,
bodies) without network access or credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence

from googlemcp.services.base import JSON, ServiceAdapter, ToolRegistry, UpstreamError


@dataclass
class RecordedRequest:
    method: str
    uri: str
    body: Any


@dataclass
class FakeContext:
    """Queue canned responses with ``respond``; inspect ``requests`` after."""

    responses: list[tuple[dict[str, str], str]] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def respond(self, payload: JSON | str, status: int = 200) -> FakeContext:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.append(({"status": str(status)}, body))
        return self

    def client(self, api: str, version: str) -> Any:
        return build(api, version, http=self._http(), static_discovery=True, cache_discovery=False)

    def execute(self, request: Any) -> JSON:
        self.requests.append(
            RecordedRequest(method=request.method, uri=request.uri, body=_decode(request.body))
        )
        try:
            result: JSON = request.execute(http=self._http())
        except HttpError as exc:
            raise UpstreamError(
                f"Google API error {exc.resp.status}", status=int(exc.resp.status)
            ) from exc
        return result

    def authorized_session(self) -> Any:
        raise NotImplementedError("use responses/requests-mock style fakes for REST-only APIs")

    def _http(self) -> HttpMockSequence:
        # Each execute consumes from the shared queue in order.
        seq = HttpMockSequence(self.responses)
        return seq


def _decode(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return body


def registry_for(adapter: ServiceAdapter, ctx: Any) -> ToolRegistry:
    registry = ToolRegistry()
    adapter.register(registry, ctx)
    return registry


def call(registry: ToolRegistry, name: str, /, **kwargs: Any) -> JSON:
    for tool in registry.tools:
        if tool.name == name:
            return tool.fn(**kwargs)
    raise KeyError(name)
