"""Runtime configuration.

Values come from, in order of precedence: explicit keyword arguments,
``GOOGLEMCP_*`` environment variables, an optional TOML file, then defaults.
The TOML file is ``$GOOGLEMCP_CONFIG`` when set, otherwise
``~/.config/googlemcp/config.toml`` when it exists.

List values accept JSON (``["gmail", "drive"]``) or a comma-separated string
(``gmail,drive``) in the environment.

Configuration never holds secret values. Secrets live in a ``SecretStore``.

The server runs locally over stdio only. Settings for the removed HTTP
transport and MCP access control are rejected with a clear error wherever
they appear (keyword, environment, or TOML), so stale deployment config
cannot be silently ignored.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from googlemcp.catalog import load_catalog

DEFAULT_CONFIG_PATH = Path("~/.config/googlemcp/config.toml")
DEFAULT_SECRET_FILE = Path("~/.config/googlemcp/secrets.json")
ALL = "all"

SecretBackend = Literal["keyring", "file", "dir", "env", "secretmanager"]


class ConfigError(ValueError):
    """Configuration is invalid or unsafe. Message is safe to print."""


LOCAL_ONLY = "is no longer supported: GoogleMCP runs locally over stdio only"
# Settings removed when the server became local-only (2026-10-06). Values are
# never echoed: some of the matching env vars used to hold secrets.
REMOVED_SETTINGS = frozenset(
    {
        "transport",
        "http_host",
        "http_port",
        "http_path",
        "http_json_response",
        "auth_mode",
        "allow_insecure_local",
        "allowed_emails",
        "public_base_url",
        "oauth_redirect_uris",
        "allow_ephemeral_oauth_state",
    }
)
# Former env backend secrets for MCP access control.
REMOVED_SECRET_ENV = frozenset(
    {
        "GOOGLEMCP_MCP_BEARER_TOKEN",
        "GOOGLEMCP_MCP_OAUTH_CLIENT_ID",
        "GOOGLEMCP_MCP_OAUTH_CLIENT_SECRET",
        "GOOGLEMCP_MCP_JWT_SIGNING_KEY",
    }
)


def removed_message(names: Sequence[str]) -> str:
    """One clause per removed setting, for example
    ``GOOGLEMCP_TRANSPORT is no longer supported: GoogleMCP runs locally over stdio only``."""
    return "; ".join(f"{name} {LOCAL_ONLY}" for name in names)


def removed_settings_in_use(data: Mapping[str, Any], environ: Mapping[str, str]) -> list[str]:
    """Names of removed settings present in ``data`` or the environment.

    ``transport = "stdio"`` (keyword, TOML, or ``GOOGLEMCP_TRANSPORT``) is
    tolerated because it only restates the one supported transport; any other
    value is reported.
    """
    found = sorted(k for k in data if k in REMOVED_SETTINGS)
    for name in sorted(REMOVED_SETTINGS):
        env = f"GOOGLEMCP_{name.upper()}"
        if env not in environ:
            continue
        if name == "transport" and environ[env].strip().lower() == "stdio":
            continue
        found.append(env)
    found += sorted(name for name in REMOVED_SECRET_ENV if name in environ)
    return found


def _split_list(value: Any) -> Any:
    if value is None or isinstance(value, list | tuple | set | frozenset):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


def config_file_path() -> Path | None:
    explicit = os.environ.get("GOOGLEMCP_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"GOOGLEMCP_CONFIG points to a missing file: {path}")
        return path
    default = DEFAULT_CONFIG_PATH.expanduser()
    return default if default.is_file() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GOOGLEMCP_",
        extra="forbid",
        validate_default=True,
    )

    # Products and policy.
    products: Annotated[list[str] | None, NoDecode] = Field(
        default=None,
        description="Enabled product keys. None means the catalog default_enabled set.",
    )
    write_products: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Products with write tools and write scopes enabled; 'all' for every one.",
    )
    allow_destructive: bool = Field(
        default=False,
        description="Expose destructive tools (delete, trash, send) for write-enabled products.",
    )

    # Secrets.
    secret_backend: SecretBackend = "keyring"  # noqa: S105 (backend name)
    secret_prefix: str = Field(default="googlemcp", pattern=r"^[a-z][a-z0-9-]{0,40}$")
    secret_file: Path = DEFAULT_SECRET_FILE
    secrets_dir: Path = Field(
        default=Path("~/.config/googlemcp/secrets.d"),
        description="Directory of secret files for the dir backend, one file per key.",
    )
    gcp_project: str | None = None

    # Google API calls and diagnostics.
    max_retries: int = Field(default=4, ge=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = config_file_path()
        if path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=path))
        return tuple(sources)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed(cls, data: Any) -> Any:
        if isinstance(data, dict) and str(data.get("transport", "")).lower() == "stdio":
            data = {k: v for k, v in data.items() if k != "transport"}
        removed = removed_settings_in_use(data if isinstance(data, dict) else {}, os.environ)
        if removed:
            raise ValueError(removed_message(removed))
        return data

    @field_validator("products", "write_products", mode="before")
    @classmethod
    def _parse_lists(cls, value: Any) -> Any:
        return _split_list(value)

    @model_validator(mode="after")
    def _check_products(self) -> Settings:
        catalog = load_catalog()
        unknown = sorted(set(self.products or ()) - set(catalog))
        unknown += sorted(set(self.write_products) - set(catalog) - {ALL})
        if unknown:
            raise ValueError(f"unknown product keys: {', '.join(unknown)}")
        return self

    # Derived views.

    def enabled_products(self) -> list[str]:
        catalog = load_catalog()
        if self.products is None:
            return sorted(k for k, p in catalog.items() if p.default_enabled)
        return sorted(set(self.products))

    def writes_enabled(self, product: str) -> bool:
        if product not in self.enabled_products():
            return False
        return ALL in self.write_products or product in self.write_products

    def secret_file_path(self) -> Path:
        return self.secret_file.expanduser()


def load_settings(**overrides: Any) -> Settings:
    """Build settings, converting validation failures into ``ConfigError``."""
    from pydantic import ValidationError

    # Report removed settings from keywords and the environment verbatim; the
    # model validator repeats the check for TOML files and direct construction.
    removed = removed_settings_in_use(
        {k: v for k, v in overrides.items() if not (k == "transport" and v == "stdio")},
        os.environ,
    )
    if removed:
        raise ConfigError(removed_message(removed))
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(_format_validation(exc)) from None


def _format_validation(exc: Any) -> str:
    parts: Sequence[str] = [
        f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
        for err in exc.errors()
    ]
    return "invalid configuration: " + "; ".join(parts)
