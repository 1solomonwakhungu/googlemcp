"""Service adapters.

Each Google product lives in its own module ``googlemcp.services.<key>`` and
exposes a module-level ``ADAPTER`` object implementing ``ServiceAdapter``.
Discovery is automatic, so adding a product never edits a shared list.
"""

from __future__ import annotations

import importlib
import pkgutil

from googlemcp.services.base import ServiceAdapter

_SKIP = {"base"}


def discover_adapters() -> dict[str, ServiceAdapter]:
    """Import every adapter module in this package and return them by key."""
    found: dict[str, ServiceAdapter] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name in _SKIP or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        adapter = getattr(module, "ADAPTER", None)
        if adapter is None:
            continue
        key = adapter.spec.key
        if key in found:
            raise RuntimeError(f"duplicate adapter key {key!r} in {info.name}")
        found[key] = adapter
    return found
