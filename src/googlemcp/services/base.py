"""Adapter contract shared by every Google product module.

Adapters never import the MCP framework. They describe tools as ``ToolDef``
records on a ``ToolRegistry``; the server core decides which tools to expose
(policy: enabled products, read-only mode, destructive opt-in) and wires them
into the MCP transport. This keeps adapters unit-testable offline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

JSON = dict[str, Any]

# Hard ceiling for list-style tools. Adapters clamp caller input to this.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class GoogleMCPError(Exception):
    """Base error. Messages must be safe to show to the model and user."""


class PolicyError(GoogleMCPError):
    """A request violated local policy (disabled product, write in read-only mode)."""


class UpstreamError(GoogleMCPError):
    """A Google API call failed. ``status`` is the HTTP status when known."""

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class ServiceSpec:
    """Static description of one Google product adapter.

    ``key`` and ``api_services`` must match ``catalog/services.json``.
    ``read_scopes`` are requested when the product is enabled; ``write_scopes``
    are requested only when writes are enabled for the product. Prefer the
    narrowest scope that supports the tools you register.
    """

    key: str
    title: str
    api_services: tuple[str, ...]
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolDef:
    """One MCP tool.

    name: ``<service key>_<verb>_<noun>``, snake_case, globally unique.
    fn: plain synchronous callable with type-annotated keyword parameters and
        a docstring; it returns a JSON-serializable dict. The core runs it in a
        worker thread.
    write: the tool mutates Google state. Hidden unless writes are enabled.
    destructive: the tool deletes, trashes, sends, or otherwise cannot be
        cleanly undone. Hidden unless destructive tools are also enabled.
    """

    name: str
    fn: Callable[..., JSON]
    description: str
    write: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True


@dataclass
class ToolRegistry:
    tools: list[ToolDef] = field(default_factory=list)

    def add(self, tool: ToolDef) -> ToolDef:
        if any(t.name == tool.name for t in self.tools):
            raise ValueError(f"duplicate tool name {tool.name!r}")
        if tool.destructive and not tool.write:
            raise ValueError(f"{tool.name}: destructive tools must also be write tools")
        self.tools.append(tool)
        return tool

    def tool(
        self,
        name: str,
        *,
        write: bool = False,
        destructive: bool = False,
        idempotent: bool = False,
        description: str | None = None,
    ) -> Callable[[Callable[..., JSON]], Callable[..., JSON]]:
        """Decorator form of ``add``. Description defaults to the docstring."""

        def decorate(fn: Callable[..., JSON]) -> Callable[..., JSON]:
            desc = description or (fn.__doc__ or "").strip()
            if not desc:
                raise ValueError(f"{name}: tools need a description or docstring")
            self.add(
                ToolDef(
                    name=name,
                    fn=fn,
                    description=desc,
                    write=write,
                    destructive=destructive,
                    idempotent=idempotent,
                )
            )
            return fn

        return decorate

    def names(self) -> list[str]:
        return [t.name for t in self.tools]


@runtime_checkable
class ServiceContext(Protocol):
    """What an adapter may use at call time. Implemented by the core."""

    def client(self, api: str, version: str) -> Any:
        """Return an authorized googleapiclient discovery Resource.

        Built with static discovery documents, so no network call happens at
        build time. Credentials refresh automatically.
        """
        ...

    def execute(self, request: Any) -> JSON:
        """Execute a googleapiclient request with retry and error mapping.

        Raises ``UpstreamError`` with a sanitized message on failure.
        """
        ...

    def authorized_session(self) -> Any:
        """Return a ``google.auth.transport.requests.AuthorizedSession`` for
        REST endpoints that lack a static discovery document (for example the
        Photos Picker API)."""
        ...


@runtime_checkable
class ServiceAdapter(Protocol):
    spec: ServiceSpec

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        """Add this product's tools to ``registry``. Must not perform I/O."""
        ...


def clamp_page_size(value: int | None) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(value), MAX_PAGE_SIZE))


def pick(source: JSON, keys: Iterable[str]) -> JSON:
    """Return only ``keys`` present in ``source``. Use to keep outputs small."""
    return {k: source[k] for k in keys if k in source}
