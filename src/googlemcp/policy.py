"""Exposure policy: which products, scopes, and tools the server offers.

Rules (see docs/architecture.md, decision 3):

- A product is active when it is enabled in config and has an adapter.
- Read scopes are requested for every active product; write scopes only for
  products with writes enabled.
- Write tools are hidden unless writes are enabled for their product.
- Destructive tools additionally need ``allow_destructive``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from googlemcp.catalog import load_catalog
from googlemcp.config import Settings
from googlemcp.services.base import ServiceAdapter, ServiceContext, ToolDef, ToolRegistry


@dataclass(frozen=True)
class ProductState:
    key: str
    title: str
    enabled: bool
    default_enabled: bool
    writes: bool
    has_adapter: bool
    risk: str

    @property
    def active(self) -> bool:
        return self.enabled and self.has_adapter


def product_states(
    settings: Settings, adapters: Mapping[str, ServiceAdapter]
) -> list[ProductState]:
    enabled = set(settings.enabled_products())
    return [
        ProductState(
            key=key,
            title=product.title,
            enabled=key in enabled,
            default_enabled=product.default_enabled,
            writes=settings.writes_enabled(key),
            has_adapter=key in adapters,
            risk=product.risk,
        )
        for key, product in sorted(load_catalog().items())
    ]


def active_adapters(
    settings: Settings, adapters: Mapping[str, ServiceAdapter]
) -> dict[str, ServiceAdapter]:
    enabled = set(settings.enabled_products())
    return {key: adapter for key, adapter in sorted(adapters.items()) if key in enabled}


def required_scopes(settings: Settings, adapters: Mapping[str, ServiceAdapter]) -> list[str]:
    """Union of read scopes for active products plus write scopes where enabled."""
    scopes: set[str] = set()
    for key, adapter in active_adapters(settings, adapters).items():
        scopes.update(adapter.spec.read_scopes)
        if settings.writes_enabled(key):
            scopes.update(adapter.spec.write_scopes)
    return sorted(scopes)


def tool_allowed(tool: ToolDef, product: str, settings: Settings) -> bool:
    if tool.write and not settings.writes_enabled(product):
        return False
    return not (tool.destructive and not settings.allow_destructive)


def select_tools(
    settings: Settings,
    adapters: Mapping[str, ServiceAdapter],
    ctx: ServiceContext,
) -> list[tuple[str, ToolDef]]:
    """Register every active adapter and return the tools policy allows."""
    selected: list[tuple[str, ToolDef]] = []
    for key, adapter in active_adapters(settings, adapters).items():
        registry = ToolRegistry()
        adapter.register(registry, ctx)
        selected.extend((key, tool) for tool in registry.tools if tool_allowed(tool, key, settings))
    return selected
