"""GoogleMCP: scoped Google API access over the Model Context Protocol."""

from importlib.metadata import PackageNotFoundError, version

# Reported by a source tree that was never installed (no package metadata).
UNKNOWN_VERSION = "0.0.0+unknown"


def _resolve_version(distribution: str = "googlemcp") -> str:
    """Installed package version, so ``pyproject.toml`` is the only place to bump it."""
    try:
        return version(distribution)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


__version__ = _resolve_version()
