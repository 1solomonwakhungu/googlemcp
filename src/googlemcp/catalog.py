"""Loader for catalog/services.json, the shared product catalog.

Terraform and the MCP adapters both read this file so API enablement and
tool registration cannot drift apart silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

_REPO_CATALOG = Path(__file__).resolve().parents[2] / "catalog" / "services.json"


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    api_services: tuple[str, ...]
    default_enabled: bool
    risk: str


def _read_raw() -> dict[str, Any]:
    if _REPO_CATALOG.is_file():
        data: dict[str, Any] = json.loads(_REPO_CATALOG.read_text(encoding="utf-8"))
        return data
    packaged = resources.files("googlemcp").joinpath("services.json")
    data = json.loads(packaged.read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Product]:
    raw = _read_raw()
    return {
        key: Product(
            key=key,
            title=entry["title"],
            api_services=tuple(entry["api_services"]),
            default_enabled=bool(entry["default_enabled"]),
            risk=str(entry["risk"]),
        )
        for key, entry in raw["products"].items()
    }
