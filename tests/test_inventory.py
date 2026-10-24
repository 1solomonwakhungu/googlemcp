"""The committed docs/inventory.md must match what scripts/gen_inventory.py renders."""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from googlemcp.catalog import Product, load_catalog
from googlemcp.services import discover_adapters

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_inventory.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_inventory", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load()


def test_committed_inventory_is_current() -> None:
    expected = gen.render(load_catalog(), discover_adapters())
    actual = gen.DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert actual == expected, f"docs/inventory.md is out of date; {gen.REGEN_HINT}"


def test_check_mode_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "inventory.md"
    assert gen.main(["--check", "--output", str(out)]) == 1  # missing file
    assert gen.main(["--output", str(out)]) == 0
    assert gen.main(["--check", "--output", str(out)]) == 0
    out.write_text(out.read_text(encoding="utf-8") + "stale\n", encoding="utf-8")
    assert gen.main(["--check", "--output", str(out)]) == 1
    assert gen.REGEN_HINT in capsys.readouterr().err


def test_render_is_deterministic() -> None:
    catalog, adapters = load_catalog(), discover_adapters()
    assert gen.render(catalog, adapters) == gen.render(catalog, adapters)


def test_missing_adapter_renders_pending() -> None:
    catalog = {
        "tasks": load_catalog()["tasks"],
        "future": Product(
            key="future",
            title="Future Product",
            api_services=("future.googleapis.com",),
            default_enabled=True,
            risk="standard",
        ),
    }
    text = gen.render(catalog, {"tasks": discover_adapters()["tasks"]})
    assert "### Future Product (`future`)" in text
    assert "adapter pending" in text
    assert "### Adapters pending\n\n`future`" in text
    assert "`tasks_create_task` | write |" in text


def _bullets(text: str, heading: str) -> set[str]:
    section = text.split(heading, 1)[1].split("\n#", 1)[0]
    return set(re.findall(r"^- `([^`]+)`", section, flags=re.MULTILINE))


def test_summary_unions_cover_default_enabled_products() -> None:
    catalog, adapters = load_catalog(), discover_adapters()
    enabled = [a.spec for k, a in adapters.items() if catalog[k].default_enabled]
    want_read = {s for spec in enabled for s in spec.read_scopes}
    want_full = want_read | {s for spec in enabled for s in spec.write_scopes}

    text = gen.render(catalog, adapters)
    assert _bullets(text, "### Read-only (default-enabled products)") == want_read
    assert _bullets(text, "### Read and write (default-enabled products)") == want_full


def test_every_catalog_product_is_listed() -> None:
    text = gen.render(load_catalog(), discover_adapters())
    for key, product in load_catalog().items():
        assert f"### {product.title} (`{key}`)" in text


def test_sensitivity_map_has_no_conflicting_entries() -> None:
    entries = [scope for _, _, scopes in gen._CLASSES for scope in scopes]
    assert len(entries) == len(set(entries)) == len(gen.SENSITIVITY)
    assert all(url.startswith("https://") for _, url in gen.SENSITIVITY.values())
