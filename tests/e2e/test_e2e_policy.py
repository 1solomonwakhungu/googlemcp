"""Tool exposure through MCP with every discovered adapter.

Expectations are computed from the adapters' own registries and the catalog,
independently of ``googlemcp.policy``, so a policy regression shows up here.
"""

from __future__ import annotations

import e2e_harness
import pytest
from e2e_harness import (
    STRICT_ENV,
    FakeGoogle,
    all_adapters,
    assert_adapters_complete,
    call_tool,
    list_tools,
    make_server,
    require_adapter,
)

from googlemcp.catalog import load_catalog
from googlemcp.config import load_settings
from googlemcp.services.base import ToolDef
from googlemcp.testing import FakeContext, registry_for

ADAPTERS = all_adapters()
CATALOG = load_catalog()
TOOLS: dict[str, list[ToolDef]] = {
    key: registry_for(adapter, FakeContext()).tools for key, adapter in ADAPTERS.items()
}
DEFAULT_ON = sorted(k for k in ADAPTERS if CATALOG[k].default_enabled)
# From the catalog, so a missing default-off adapter is visible (skip or strict fail).
DEFAULT_OFF = sorted(k for k, p in CATALOG.items() if not p.default_enabled)
WITH_WRITES = sorted(k for k, tools in TOOLS.items() if any(t.write for t in tools))
WITH_DESTRUCTIVE = sorted(k for k, tools in TOOLS.items() if any(t.destructive for t in tools))


def _names(product: str, *, write: bool, destructive: bool = False) -> set[str]:
    return {t.name for t in TOOLS[product] if t.write == write and t.destructive == destructive}


def _reads(products: list[str]) -> set[str]:
    return set().union(*(_names(p, write=False) for p in products))


def test_every_adapter_is_in_the_catalog() -> None:
    assert ADAPTERS, "no adapters discovered"
    assert set(ADAPTERS) <= set(CATALOG)


def test_every_catalog_product_has_an_adapter() -> None:
    """Strict mode (main): set(ADAPTERS) == set(CATALOG). Elsewhere: skip."""
    assert_adapters_complete()
    assert set(ADAPTERS) == set(CATALOG)


@pytest.fixture
def without_one_adapter(monkeypatch: pytest.MonkeyPatch) -> str:
    present = all_adapters()
    victim = sorted(present)[0]
    trimmed = {k: v for k, v in present.items() if k != victim}
    monkeypatch.setattr(e2e_harness, "discover_adapters", lambda: trimmed)
    return victim


def test_strict_mode_fails_when_an_adapter_is_missing(
    monkeypatch: pytest.MonkeyPatch, without_one_adapter: str
) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=without_one_adapter):
        assert_adapters_complete()
    with pytest.raises(pytest.fail.Exception, match=without_one_adapter):
        require_adapter(without_one_adapter)


def test_non_strict_mode_skips_when_an_adapter_is_missing(
    monkeypatch: pytest.MonkeyPatch, without_one_adapter: str
) -> None:
    monkeypatch.setenv(STRICT_ENV, "0")
    with pytest.raises(pytest.skip.Exception, match=without_one_adapter):
        assert_adapters_complete()


def test_default_listing_is_read_only_and_default_products_only() -> None:
    listed = list_tools(make_server(load_settings(), FakeGoogle()))
    assert set(listed) == _reads(DEFAULT_ON)
    for key in DEFAULT_ON:
        assert _names(key, write=False) <= set(listed), f"{key} has no visible read tools"
    for tool in listed.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


@pytest.mark.parametrize("product", DEFAULT_OFF)
def test_default_off_product_absent_until_enabled(product: str) -> None:
    require_adapter(product)
    default = list_tools(make_server(load_settings(), FakeGoogle()))
    assert not any(name.startswith(f"{product}_") for name in default)

    enabled = list_tools(make_server(load_settings(products=[product]), FakeGoogle()))
    assert set(enabled) == _names(product, write=False)


@pytest.mark.parametrize("product", WITH_WRITES)
def test_per_product_write_enable(product: str) -> None:
    products = sorted({*DEFAULT_ON, product})
    settings = load_settings(products=products, write_products=[product])
    listed = list_tools(make_server(settings, FakeGoogle()))

    expected = _reads(products) | _names(product, write=True)
    assert set(listed) == expected
    for name in _names(product, write=True):
        assert listed[name].annotations.read_only_hint is False
        assert listed[name].annotations.destructive_hint is False


@pytest.mark.parametrize("product", WITH_DESTRUCTIVE)
def test_destructive_tools_need_second_opt_in(product: str) -> None:
    destructive = _names(product, write=True, destructive=True)
    base = {"products": [product], "write_products": [product]}

    without = list_tools(make_server(load_settings(**base), FakeGoogle()))
    assert not destructive & set(without)

    with_opt_in = list_tools(
        make_server(load_settings(**base, allow_destructive=True), FakeGoogle())
    )
    assert destructive <= set(with_opt_in)
    for name in destructive:
        assert with_opt_in[name].annotations.destructive_hint is True


def test_destructive_opt_in_alone_exposes_nothing_new() -> None:
    plain = list_tools(make_server(load_settings(), FakeGoogle()))
    opted = list_tools(make_server(load_settings(allow_destructive=True), FakeGoogle()))
    assert set(opted) == set(plain)


@pytest.mark.parametrize("product", WITH_WRITES)
def test_hidden_write_tools_cannot_be_called(product: str) -> None:
    google = FakeGoogle()
    mcp = make_server(load_settings(products=[product]), google)
    hidden = {t.name for t in TOOLS[product] if t.write}
    for name in sorted(hidden):
        result = call_tool(mcp, name, {})
        assert result.is_error, name
    assert google.requests == []
