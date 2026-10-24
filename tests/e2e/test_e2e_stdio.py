"""The real ``googlemcp serve`` process over stdio, the only transport.

The server runs as a subprocess through ``stdio_launcher.py``, which blocks
and logs every outbound network attempt. Credentials are fake values in a
temporary 0600 file secret store and ``HOME`` points at a temp directory, so
no real user config, keyring, or Google account is touched.

Why tools are not called successfully here: a subprocess cannot be handed
the in-process fake Google transport, so a real tool call would try to
reach Google. The guard test below relies on exactly that to prove the
network block works; successful tool calls are covered in-process by
test_e2e_tools.py with the same server code.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    FakeGoogle,
    assert_no_secrets,
    list_tools,
    make_server,
)
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from googlemcp.config import REMOVED_SECRET_ENV, REMOVED_SETTINGS, load_settings

LAUNCHER = Path(__file__).with_name("stdio_launcher.py")


def _secret_file(root: Path) -> Path:
    path = root / "secrets.json"
    path.write_text(
        json.dumps(
            {
                "oauth-client-id": CLIENT_ID,
                "oauth-client-secret": CLIENT_SECRET,
                "refresh-token": REFRESH_TOKEN,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _env(tmp_path: Path, **env: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "GOOGLEMCP_SECRET_BACKEND": "file",
        "GOOGLEMCP_SECRET_FILE": str(_secret_file(tmp_path)),
    }
    return {**base, **env}


def _transport(tmp_path: Path, log: Path, **env: str) -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=[str(LAUNCHER), str(log), "serve"],
        env=_env(tmp_path, **env),
        cwd=str(tmp_path),
    )


def _run(tmp_path: Path, log: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the launcher to completion with stdin closed."""
    return subprocess.run(  # noqa: S603 (fixed interpreter and test-owned script)
        [sys.executable, str(LAUNCHER), str(log), *args],
        env=_env(tmp_path, **env),
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _session(transport: StdioTransport, call: tuple[str, dict[str, Any]] | None = None) -> Any:
    async def run() -> Any:
        async with Client(transport) as client:
            info = client.server_info
            tools = {t.name: t for t in await client.list_tools()}
            result = None
            if call is not None:
                result = await client.call_tool(call[0], call[1], raise_on_error=False)
            return info, tools, result

    return asyncio.run(run())


def _log_lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _attempts(log: Path) -> list[str]:
    """Network attempts, excluding the launcher's known ``expected-`` entries.

    The only expected entry is urllib3's import-time IPv6 probe, which the
    launcher still refuses; anything else in that category fails here.
    """
    lines = _log_lines(log)
    expected = [line for line in lines if line.startswith("expected-")]
    assert all(line == "expected-bind ('::1', 0)" for line in expected), expected
    return [line for line in lines if not line.startswith("expected-")]


def test_stdio_initialize_and_list_tools_without_network(tmp_path: Path) -> None:
    log = tmp_path / "network-attempts.log"
    info, tools, _ = _session(_transport(tmp_path, log))

    assert info is not None
    assert info.name == "googlemcp"
    expected = set(list_tools(make_server(load_settings(), FakeGoogle())))
    assert set(tools) == expected, "stdio process must expose the default read-only set"
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
    assert _attempts(log) == [], "startup, initialize, and tools/list must not touch the network"


def test_stdio_policy_comes_from_environment(tmp_path: Path) -> None:
    log = tmp_path / "network-attempts.log"
    env = {"GOOGLEMCP_PRODUCTS": "tasks", "GOOGLEMCP_WRITE_PRODUCTS": "tasks"}
    _, tools, _ = _session(_transport(tmp_path, log, **env))

    in_process = make_server(
        load_settings(products=["tasks"], write_products=["tasks"]), FakeGoogle()
    )
    assert set(tools) == set(list_tools(in_process))
    assert "tasks_create_task" in tools
    assert "tasks_delete_task" not in tools, "destructive tools need the second opt-in"
    assert all(name.startswith("tasks_") for name in tools)
    assert _attempts(log) == []


def test_stdio_network_guard_is_effective(tmp_path: Path) -> None:
    """A real tool call must try to reach Google; the guard blocks and logs it."""
    log = tmp_path / "network-attempts.log"
    _, _, result = _session(
        _transport(tmp_path, log, GOOGLEMCP_PRODUCTS="tasks"),
        call=("tasks_list_tasklists", {}),
    )

    assert result.is_error
    attempts = _attempts(log)
    assert attempts, "the guard saw no network attempt, so it would not catch one either"
    assert any("googleapis.com" in line for line in attempts), attempts
    text = "\n".join(getattr(b, "text", "") for b in result.content)
    assert_no_secrets([text, *attempts])


# Removed setting names. The test also follows googlemcp.config, so a setting
# added to the removed set later is covered automatically, while one dropped
# from it fails here.
CONTRACT_REMOVED = {
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
    "GOOGLEMCP_MCP_BEARER_TOKEN",
    "GOOGLEMCP_MCP_OAUTH_CLIENT_ID",
    "GOOGLEMCP_MCP_OAUTH_CLIENT_SECRET",
    "GOOGLEMCP_MCP_JWT_SIGNING_KEY",
}
REMOVED_ENV = sorted(
    {f"GOOGLEMCP_{name.upper()}" for name in REMOVED_SETTINGS} | set(REMOVED_SECRET_ENV)
)
SENTINEL_VALUE = "e2e-removed-value-7c1d"


def test_removed_settings_cover_the_contract() -> None:
    assert set(REMOVED_ENV) >= CONTRACT_REMOVED


@pytest.mark.parametrize("name", REMOVED_ENV)
def test_removed_setting_refuses_to_start(tmp_path: Path, name: str) -> None:
    """Local-only: each removed setting stops startup with the exact message."""
    log = tmp_path / "network-attempts.log"
    value = "http" if name == "GOOGLEMCP_TRANSPORT" else SENTINEL_VALUE
    proc = _run(tmp_path, log, "serve", **{name: value})

    assert proc.returncode == 2, proc.stderr
    expected = f"error: {name} is no longer supported: GoogleMCP runs locally over stdio only"
    assert expected in proc.stderr.splitlines(), proc.stderr
    assert proc.stdout == "", "no MCP traffic may be produced"
    assert SENTINEL_VALUE not in proc.stderr, "removed values must not be echoed"
    assert _attempts(log) == []


def test_all_removed_settings_are_reported_together(tmp_path: Path) -> None:
    log = tmp_path / "network-attempts.log"
    env = {"GOOGLEMCP_TRANSPORT": "http", "GOOGLEMCP_HTTP_PORT": "8080"}
    proc = _run(tmp_path, log, "serve", **env)

    assert proc.returncode == 2, proc.stderr
    expected = (
        "error: GOOGLEMCP_HTTP_PORT is no longer supported: GoogleMCP runs locally over stdio"
        " only; GOOGLEMCP_TRANSPORT is no longer supported: GoogleMCP runs locally over stdio"
        " only"
    )
    assert expected in proc.stderr.splitlines(), proc.stderr
    assert _attempts(log) == []


def test_transport_stdio_is_tolerated(tmp_path: Path) -> None:
    """Restating the one supported transport (older MCP client configs) still works."""
    log = tmp_path / "network-attempts.log"
    info, tools, _ = _session(_transport(tmp_path, log, GOOGLEMCP_TRANSPORT="stdio"))
    assert info is not None
    assert tools
    assert _attempts(log) == []


def test_launcher_guard_catches_every_kind(tmp_path: Path) -> None:
    """Positive control: deliberate bind, listen, DNS, connect, and send are refused."""
    log = tmp_path / "network-attempts.log"
    proc = _run(tmp_path, log, "--probe")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    kinds = {
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "create_connection",
        "bind",
        "listen",
        "connect",
        "connect_ex",
        "sendto",
        "sendmsg",
        "gethostbyaddr",
        "getnameinfo",
    }
    denied = {line.split(" ", 1)[1] for line in proc.stdout.splitlines()}
    assert denied == kinds
    logged = {line.split(" ", 1)[0] for line in _log_lines(log)}
    assert logged == kinds, "the probe is not urllib3, so nothing may be classed expected"
