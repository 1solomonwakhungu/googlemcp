"""Cross-checks every adapter against the shared catalog and naming rules."""

import re

import pytest

from googlemcp.catalog import load_catalog
from googlemcp.services import discover_adapters
from googlemcp.services.base import ServiceAdapter
from googlemcp.testing import FakeContext, registry_for

ADAPTERS = discover_adapters()
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def test_at_least_one_adapter() -> None:
    assert ADAPTERS


@pytest.mark.parametrize("key", sorted(ADAPTERS))
def test_adapter_matches_catalog(key: str) -> None:
    catalog = load_catalog()
    adapter = ADAPTERS[key]
    assert isinstance(adapter, ServiceAdapter)
    assert key in catalog, f"{key} missing from catalog/services.json"
    assert adapter.spec.api_services == catalog[key].api_services
    assert adapter.spec.read_scopes, "every adapter needs at least one read scope"


@pytest.mark.parametrize("key", sorted(ADAPTERS))
def test_tool_names_are_prefixed_and_valid(key: str) -> None:
    reg = registry_for(ADAPTERS[key], FakeContext())
    assert reg.tools, f"{key} registers no tools"
    for tool in reg.tools:
        assert NAME_RE.match(tool.name), tool.name
        assert tool.name.startswith(f"{key}_"), tool.name
        assert tool.description


def test_tool_names_globally_unique() -> None:
    seen: set[str] = set()
    for adapter in ADAPTERS.values():
        for tool in registry_for(adapter, FakeContext()).tools:
            assert tool.name not in seen, tool.name
            seen.add(tool.name)
