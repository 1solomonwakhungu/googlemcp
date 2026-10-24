from pathlib import Path

import pytest

from googlemcp.catalog import load_catalog
from googlemcp.config import REMOVED_SETTINGS, ConfigError, Settings, load_settings


def test_defaults_follow_catalog() -> None:
    settings = load_settings()
    expected = sorted(k for k, p in load_catalog().items() if p.default_enabled)
    assert settings.enabled_products() == expected
    assert "chat" not in settings.enabled_products()
    assert settings.secret_backend == "keyring"
    assert not settings.allow_destructive
    assert not any(settings.writes_enabled(p) for p in expected)


def test_settings_have_no_http_or_auth_fields() -> None:
    assert not REMOVED_SETTINGS & set(Settings.model_fields)


def test_env_lists_accept_csv_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks, drive")
    monkeypatch.setenv("GOOGLEMCP_WRITE_PRODUCTS", '["tasks"]')
    settings = load_settings()
    assert settings.enabled_products() == ["drive", "tasks"]
    assert settings.writes_enabled("tasks")
    assert not settings.writes_enabled("drive")


def test_write_all_only_covers_enabled_products() -> None:
    settings = load_settings(products=["tasks"], write_products=["all"])
    assert settings.writes_enabled("tasks")
    assert not settings.writes_enabled("gmail")


def test_unknown_product_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown product keys: nope"):
        load_settings(products=["nope"])


def test_toml_file_has_lower_precedence_than_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('products = ["gmail"]\nmax_retries = 2\nlog_level = "DEBUG"\n')
    monkeypatch.setenv("GOOGLEMCP_CONFIG", str(config))
    monkeypatch.setenv("GOOGLEMCP_MAX_RETRIES", "7")
    settings = load_settings()
    assert settings.enabled_products() == ["gmail"]
    assert settings.log_level == "DEBUG"
    assert settings.max_retries == 7


def test_missing_explicit_config_file_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOOGLEMCP_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError, match="missing file"):
        load_settings()


def test_dir_backend_default_is_local() -> None:
    assert load_settings().secrets_dir == Path("~/.config/googlemcp/secrets.d")


# Removed HTTP and MCP access-control settings are rejected, never ignored.


@pytest.mark.parametrize(
    "env",
    [
        "GOOGLEMCP_TRANSPORT",
        "GOOGLEMCP_HTTP_HOST",
        "GOOGLEMCP_HTTP_PORT",
        "GOOGLEMCP_HTTP_PATH",
        "GOOGLEMCP_HTTP_JSON_RESPONSE",
        "GOOGLEMCP_AUTH_MODE",
        "GOOGLEMCP_ALLOW_INSECURE_LOCAL",
        "GOOGLEMCP_ALLOWED_EMAILS",
        "GOOGLEMCP_PUBLIC_BASE_URL",
        "GOOGLEMCP_OAUTH_REDIRECT_URIS",
        "GOOGLEMCP_ALLOW_EPHEMERAL_OAUTH_STATE",
    ],
)
def test_removed_env_settings_rejected(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv(env, "http" if env == "GOOGLEMCP_TRANSPORT" else "x")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert (
        str(info.value) == f"{env} is no longer supported: GoogleMCP runs locally over stdio only"
    )


def test_transport_http_rejected_with_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLEMCP_TRANSPORT", "http")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert str(info.value) == (
        "GOOGLEMCP_TRANSPORT is no longer supported: GoogleMCP runs locally over stdio only"
    )


def test_several_removed_settings_are_all_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLEMCP_TRANSPORT", "http")
    monkeypatch.setenv("GOOGLEMCP_HTTP_PORT", "8080")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert str(info.value) == (
        "GOOGLEMCP_HTTP_PORT is no longer supported: GoogleMCP runs locally over stdio only; "
        "GOOGLEMCP_TRANSPORT is no longer supported: GoogleMCP runs locally over stdio only"
    )


@pytest.mark.parametrize("value", ["stdio", "STDIO", " stdio "])
def test_transport_stdio_env_is_tolerated(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GOOGLEMCP_TRANSPORT", value)
    load_settings()


@pytest.mark.parametrize(
    "env",
    [
        "GOOGLEMCP_MCP_BEARER_TOKEN",
        "GOOGLEMCP_MCP_OAUTH_CLIENT_ID",
        "GOOGLEMCP_MCP_OAUTH_CLIENT_SECRET",
        "GOOGLEMCP_MCP_JWT_SIGNING_KEY",
    ],
)
def test_removed_secret_env_rejected_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv(env, "super-secret-value-123")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert env in str(info.value)
    assert "super-secret-value-123" not in str(info.value)


@pytest.mark.parametrize("field", sorted(REMOVED_SETTINGS - {"transport"}))
def test_removed_keyword_settings_rejected(field: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(**{field: "x"})
    assert (
        str(info.value) == f"{field} is no longer supported: GoogleMCP runs locally over stdio only"
    )


def test_direct_construction_also_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("GOOGLEMCP_AUTH_MODE", "bearer")
    with pytest.raises(ValidationError, match="GOOGLEMCP_AUTH_MODE is no longer supported"):
        Settings()


def test_removed_toml_settings_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('transport = "http"\nhttp_port = 9001\nproducts = ["tasks"]\n')
    monkeypatch.setenv("GOOGLEMCP_CONFIG", str(config))
    with pytest.raises(ConfigError) as info:
        load_settings()
    message = str(info.value)
    assert "http_port is no longer supported: GoogleMCP runs locally over stdio only" in message
    assert "transport is no longer supported" in message


def test_transport_stdio_in_toml_or_keyword_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert load_settings(transport="stdio").enabled_products()
    config = tmp_path / "config.toml"
    config.write_text('transport = "stdio"\nproducts = ["tasks"]\n')
    monkeypatch.setenv("GOOGLEMCP_CONFIG", str(config))
    assert load_settings().enabled_products() == ["tasks"]
