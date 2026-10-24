import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from conftest import MemoryStore, NullContext
from googlemcp import server
from googlemcp.config import Settings, load_settings
from googlemcp.server import build_server


def _server(settings: Settings, adapters: dict[str, Any]) -> Any:
    return build_server(settings, MemoryStore(), adapters=adapters, ctx=NullContext())


def _list_tools(mcp: Any) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(run())


def _call(mcp: Any, name: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return asyncio.run(run())


# Tool exposure over stdio-equivalent in-memory transport


def test_tool_listing_follows_policy(adapters: dict[str, Any]) -> None:
    read_only = _list_tools(_server(load_settings(products=["tasks"]), adapters))
    assert "tasks_fake_read" in read_only
    assert "tasks_fake_write" not in read_only
    assert "tasks_fake_delete" not in read_only
    assert not any(n.startswith("drive_") for n in read_only)

    everything = load_settings(
        products=["tasks", "drive"], write_products=["all"], allow_destructive=True
    )
    tools = _list_tools(_server(everything, adapters))
    assert {"tasks_fake_write", "tasks_fake_delete", "drive_fake_list"} <= set(tools)


def test_annotations_are_explicit(adapters: dict[str, Any]) -> None:
    settings = load_settings(products=["tasks"], write_products=["tasks"], allow_destructive=True)
    tools = _list_tools(_server(settings, adapters))
    read = tools["tasks_fake_read"].annotations
    assert read.read_only_hint is True and read.destructive_hint is False
    assert read.idempotent_hint is True and read.open_world_hint is True
    write = tools["tasks_fake_write"].annotations
    assert write.read_only_hint is False and write.destructive_hint is False
    delete = tools["tasks_fake_delete"].annotations
    assert delete.read_only_hint is False and delete.destructive_hint is True


def test_schema_keeps_adapter_parameters(adapters: dict[str, Any]) -> None:
    tool = _list_tools(_server(load_settings(products=["tasks"]), adapters))["tasks_fake_read"]
    assert set(tool.input_schema["properties"]) == {"name", "limit"}
    assert tool.description == "Read something harmless."


def test_tool_call_returns_structured_result(adapters: dict[str, Any]) -> None:
    result = _call(
        _server(load_settings(products=["tasks"]), adapters), "tasks_fake_read", {"name": "x"}
    )
    assert result.structured_content == {"hello": "x", "limit": 5}


def test_google_errors_become_tool_errors(adapters: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="404 \\(notFound\\): gone"):
        _call(_server(load_settings(products=["tasks"]), adapters), "tasks_fake_upstream_error", {})


def test_rest_adapter_403_text_is_withheld(adapters: dict[str, Any]) -> None:
    with pytest.raises(ToolError) as info:
        _call(_server(load_settings(products=["tasks"]), adapters), "tasks_fake_rest_error", {})
    assert str(info.value) == (
        "Google API error 403 (PERMISSION_DENIED). Google denied the request; details withheld."
    )


def test_rest_adapter_other_errors_are_redacted(adapters: dict[str, Any]) -> None:
    with pytest.raises(ToolError) as info:
        _call(_server(load_settings(products=["tasks"]), adapters), "tasks_fake_rest_error_500", {})
    text = str(info.value)
    assert "synthetic" not in text and "example.invalid" not in text
    assert text == 'Photos error 500: see <url> and {"access_token": "<redacted>"}'


def test_unexpected_errors_are_masked(adapters: dict[str, Any]) -> None:
    with pytest.raises(ToolError) as info:
        _call(_server(load_settings(products=["tasks"]), adapters), "tasks_fake_crash", {})
    assert "secret-internal-detail" not in str(info.value)
    assert "internal error" in str(info.value)


def test_session_refresh_errors_map_to_login_hint(adapters: dict[str, Any]) -> None:
    mcp = _server(load_settings(products=["tasks"]), adapters)
    with pytest.raises(ToolError, match="7 days"):
        _call(mcp, "tasks_fake_session_refresh", {})


def test_session_network_errors_are_sanitized(adapters: dict[str, Any]) -> None:
    mcp = _server(load_settings(products=["tasks"]), adapters)
    with pytest.raises(ToolError) as info:
        _call(mcp, "tasks_fake_session_network", {})
    assert "network error" in str(info.value) and "secret" not in str(info.value)


def test_hidden_tools_cannot_be_called(adapters: dict[str, Any]) -> None:
    with pytest.raises(ToolError):
        _call(
            _server(load_settings(products=["tasks"]), adapters), "tasks_fake_write", {"title": "t"}
        )


def test_real_tasks_adapter_registers() -> None:
    tools = _list_tools(
        build_server(load_settings(products=["tasks"]), MemoryStore(), ctx=NullContext())
    )
    assert "tasks_list_tasks" in tools
    assert "tasks_create_task" not in tools


# Local-only: stdio is the only transport and nothing serves HTTP


def test_serve_runs_stdio_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, **kw: seen.append(kw))
    server.serve(load_settings(products=["tasks"]), MemoryStore())
    assert seen == [{"transport": "stdio", "show_banner": False}]


def test_building_the_server_constructs_no_http_app(
    monkeypatch: pytest.MonkeyPatch, adapters: dict[str, Any]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an HTTP app or listener was constructed")

    monkeypatch.setattr(FastMCP, "http_app", forbidden)
    monkeypatch.setattr(FastMCP, "run_http_async", forbidden)
    mcp = _server(load_settings(products=["tasks"]), adapters)
    assert mcp.auth is None
    assert mcp._additional_http_routes == []
    assert "tasks_fake_read" in _list_tools(mcp)


def test_instructions_are_client_and_account_neutral() -> None:
    from googlemcp.server import INSTRUCTIONS

    lowered = INSTRUCTIONS.lower()
    assert "hermes" not in lowered and "owner" not in lowered


def test_package_ships_typing_marker() -> None:
    import googlemcp

    assert (Path(googlemcp.__file__).parent / "py.typed").is_file()


@pytest.mark.parametrize(
    "name",
    [
        "build_auth",
        "BearerTokenVerifier",
        "OwnerGoogleProvider",
        "email_allowed",
        "oauth_state_problem",
        "check_oauth_state",
        "resolve_home",
        "mount_for",
        "HEALTH_PATH",
    ],
)
def test_http_and_auth_symbols_are_gone(name: str) -> None:
    assert not hasattr(server, name)


def test_no_oauth_proxy_or_http_routes_in_a_fresh_process() -> None:
    code = (
        "import json, sys\n"
        "from googlemcp import cli, server\n"
        "from googlemcp.config import load_settings\n"
        "from googlemcp.secretstore import EnvStore\n"
        "mcp = server.build_server(load_settings(products=['tasks']), EnvStore({}))\n"
        "print(json.dumps({\n"
        "    'google_provider': 'fastmcp.server.auth.providers.google' in sys.modules,\n"
        "    'oauth_proxy': 'fastmcp.server.auth.oauth_proxy' in sys.modules,\n"
        "    'routes': len(mcp._additional_http_routes),\n"
        "    'auth': mcp.auth is not None,\n"
        "}))\n"
    )
    out = subprocess.run(  # noqa: S603 (fixed argv built in this test)
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(out.stdout) == {
        "google_provider": False,
        "oauth_proxy": False,
        "routes": 0,
        "auth": False,
    }


# Real stdio subprocess (the path MCP clients use)


def _stdio_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                "oauth-client-id": "fake-client.apps.googleusercontent.com",
                "oauth-client-secret": "fake-client-secret",
                "refresh-token": "fake-refresh-token",
            }
        )
    )
    secrets_file.chmod(0o600)
    return {
        "GOOGLEMCP_PRODUCTS": "tasks",
        "GOOGLEMCP_SECRET_BACKEND": "file",
        "GOOGLEMCP_SECRET_FILE": str(secrets_file),
        "HOME": str(tmp_path),
        **extra,
    }


@pytest.mark.parametrize("args", [["serve"], ["serve", "--transport", "stdio"]])
def test_stdio_subprocess_lists_tools(tmp_path: Path, args: list[str]) -> None:
    transport = StdioTransport(
        command=sys.executable, args=["-m", "googlemcp", *args], env=_stdio_env(tmp_path)
    )

    async def run() -> list[str]:
        async with Client(transport) as client:
            return [t.name for t in await client.list_tools()]

    names = asyncio.run(run())
    assert "tasks_list_tasks" in names
    assert "tasks_create_task" not in names


def test_serve_rejects_http_transport_flag(tmp_path: Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "googlemcp", "serve", "--transport", "http"],
        capture_output=True,
        text=True,
        env=_stdio_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert out.returncode == 2
    assert "invalid choice: 'http'" in out.stderr


def test_serve_rejects_http_transport_env(tmp_path: Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "googlemcp", "serve"],
        capture_output=True,
        text=True,
        env=_stdio_env(tmp_path, GOOGLEMCP_TRANSPORT="http"),
        timeout=60,
        check=False,
    )
    assert out.returncode == 2
    assert (
        "GOOGLEMCP_TRANSPORT is no longer supported: GoogleMCP runs locally over stdio only"
        in out.stderr
    )
