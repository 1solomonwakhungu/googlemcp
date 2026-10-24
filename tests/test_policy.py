from typing import Any

from conftest import NullContext
from googlemcp.config import load_settings
from googlemcp.policy import product_states, required_scopes, select_tools
from googlemcp.services import discover_adapters


def _names(settings: Any, adapters: dict[str, Any]) -> list[str]:
    return [t.name for _, t in select_tools(settings, adapters, NullContext())]


def test_scope_union_read_only(adapters: dict[str, Any]) -> None:
    settings = load_settings(products=["tasks", "drive"])
    assert required_scopes(settings, adapters) == ["drive.read", "scope.read"]


def test_write_scopes_only_for_write_enabled_products(adapters: dict[str, Any]) -> None:
    settings = load_settings(products=["tasks", "drive"], write_products=["tasks"])
    assert required_scopes(settings, adapters) == ["drive.read", "scope.read", "scope.write"]


def test_disabled_product_contributes_nothing(adapters: dict[str, Any]) -> None:
    settings = load_settings(products=["tasks"], write_products=["all"])
    assert required_scopes(settings, adapters) == ["scope.read", "scope.write"]
    assert not any(n.startswith("drive_") for n in _names(settings, adapters))


def test_read_only_hides_write_and_destructive(adapters: dict[str, Any]) -> None:
    names = _names(load_settings(products=["tasks"]), adapters)
    assert "tasks_fake_read" in names
    assert "tasks_fake_write" not in names
    assert "tasks_fake_delete" not in names


def test_writes_without_destructive_opt_in(adapters: dict[str, Any]) -> None:
    names = _names(load_settings(products=["tasks"], write_products=["tasks"]), adapters)
    assert "tasks_fake_write" in names
    assert "tasks_fake_delete" not in names


def test_destructive_needs_writes_too(adapters: dict[str, Any]) -> None:
    only_flag = _names(load_settings(products=["tasks"], allow_destructive=True), adapters)
    assert "tasks_fake_delete" not in only_flag
    both = load_settings(products=["tasks"], write_products=["tasks"], allow_destructive=True)
    assert "tasks_fake_delete" in _names(both, adapters)


def test_product_states_flag_missing_adapters(adapters: dict[str, Any]) -> None:
    states = {
        s.key: s for s in product_states(load_settings(products=["tasks", "gmail"]), adapters)
    }
    assert states["tasks"].active
    assert states["gmail"].enabled and not states["gmail"].has_adapter
    assert not states["gmail"].active
    assert not states["chat"].enabled and not states["chat"].default_enabled


def test_real_tasks_adapter_scopes() -> None:
    real = discover_adapters()
    settings = load_settings(products=["tasks"])
    assert required_scopes(settings, real) == ["https://www.googleapis.com/auth/tasks.readonly"]
    writes = load_settings(products=["tasks"], write_products=["tasks"])
    assert "https://www.googleapis.com/auth/tasks" in required_scopes(writes, real)
