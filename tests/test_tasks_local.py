"""Google Tasks through the real local server, offline.

Each test configures the server only through ``GOOGLEMCP_*`` environment
variables, builds it with ``build_server`` (real adapter discovery, policy,
and FastMCP tool wrapping), and talks to it with FastMCP's in-memory client,
which speaks the same MCP protocol as stdio. Google is replaced by
``FakeContext``, which builds real googleapiclient requests from the bundled
discovery document and records them, so no network call or credential is
involved.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client, FastMCP

from conftest import MemoryStore
from googlemcp.cli import main as cli_main
from googlemcp.config import load_settings
from googlemcp.policy import required_scopes
from googlemcp.server import build_server
from googlemcp.services import discover_adapters
from googlemcp.testing import FakeContext

READ_SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/tasks"

READ_TOOLS = {"tasks_list_tasklists", "tasks_list_tasks", "tasks_get_task"}
WRITE_TOOLS = {"tasks_create_task", "tasks_update_task", "tasks_complete_task"}
DESTRUCTIVE_TOOLS = {"tasks_delete_task"}

READ_ONLY_ENV = {"GOOGLEMCP_PRODUCTS": "tasks"}
WRITE_ENV = {**READ_ONLY_ENV, "GOOGLEMCP_WRITE_PRODUCTS": "tasks"}
DESTRUCTIVE_ENV = {**WRITE_ENV, "GOOGLEMCP_ALLOW_DESTRUCTIVE": "true"}


def _configure(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _server(ctx: FakeContext | None = None) -> FastMCP:
    return build_server(load_settings(), MemoryStore(), ctx=ctx or FakeContext())


def _tools(mcp: FastMCP) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(run())


def _tasks_tools(mcp: FastMCP) -> dict[str, Any]:
    return {name: tool for name, tool in _tools(mcp).items() if name.startswith("tasks_")}


def _scopes() -> list[str]:
    return required_scopes(load_settings(), discover_adapters())


def _cli_scopes(capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert cli_main(["scopes", "--json"]) == 0
    scopes: list[str] = json.loads(capsys.readouterr().out)
    return scopes


# (a) read-only, the default for every product


def test_read_only_mode_exposes_only_read_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, READ_ONLY_ENV)
    tools = _tools(_server())

    assert set(tools) == READ_TOOLS
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
    assert _scopes() == [READ_SCOPE]
    assert _cli_scopes(capsys) == [READ_SCOPE]


def test_default_configuration_keeps_tasks_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # No GOOGLEMCP_* variables at all: every default-enabled product is on, reads only.
    tools = _tasks_tools(_server())

    assert set(tools) == READ_TOOLS
    scopes = _scopes()
    assert READ_SCOPE in scopes
    assert WRITE_SCOPE not in scopes


# (b) writes enabled for Tasks


def test_write_mode_adds_write_tools_but_not_delete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, WRITE_ENV)
    tools = _tools(_server())

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert "tasks_delete_task" not in tools
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is False
    assert _scopes() == [WRITE_SCOPE, READ_SCOPE]
    assert _cli_scopes(capsys) == [WRITE_SCOPE, READ_SCOPE]


def test_destructive_flag_alone_does_not_enable_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, {**READ_ONLY_ENV, "GOOGLEMCP_ALLOW_DESTRUCTIVE": "true"})

    assert set(_tools(_server())) == READ_TOOLS
    assert _scopes() == [READ_SCOPE]


# (c) writes plus destructive opt-in


def test_destructive_mode_exposes_delete_marked_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, DESTRUCTIVE_ENV)
    tools = _tools(_server())

    assert set(tools) == READ_TOOLS | WRITE_TOOLS | DESTRUCTIVE_TOOLS
    delete = tools["tasks_delete_task"].annotations
    assert delete.destructive_hint is True
    assert delete.read_only_hint is False
    for name in READ_TOOLS | WRITE_TOOLS:
        assert tools[name].annotations.destructive_hint is False
    assert _scopes() == [WRITE_SCOPE, READ_SCOPE]


# (d) every tool end to end, asserting exactly what reaches Google


def _query(uri: str) -> dict[str, list[str]]:
    query = parse_qs(urlsplit(uri).query)
    query.pop("alt", None)
    return query


def test_every_tasks_tool_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, DESTRUCTIVE_ENV)
    ctx = (
        FakeContext()
        .respond({"items": [{"id": "L1", "title": "Inbox", "etag": "e"}], "nextPageToken": "p2"})
        .respond({"items": [{"id": "t1", "title": "Pay rent", "status": "needsAction"}]})
        .respond({"id": "t1", "title": "Pay rent", "status": "needsAction", "selfLink": "x"})
        .respond({"id": "t2", "title": "Call bank", "due": "2026-10-07T00:00:00.000Z"})
        .respond({"id": "t2", "title": "Call bank today", "notes": "ask about fees"})
        .respond({"id": "t2", "status": "completed", "completed": "2026-10-06T18:00:00.000Z"})
        .respond("", status=204)
    )
    mcp = _server(ctx)

    calls: list[tuple[str, dict[str, Any]]] = [
        ("tasks_list_tasklists", {"max_results": 500}),
        ("tasks_list_tasks", {"tasklist_id": "L1", "show_completed": True}),
        ("tasks_get_task", {"tasklist_id": "L1", "task_id": "t1"}),
        ("tasks_create_task", {"tasklist_id": "L1", "title": "Call bank", "due": "2026-10-07"}),
        (
            "tasks_update_task",
            {
                "tasklist_id": "L1",
                "task_id": "t2",
                "title": "Call bank today",
                "notes": "ask about fees",
            },
        ),
        ("tasks_complete_task", {"tasklist_id": "L1", "task_id": "t2"}),
        ("tasks_delete_task", {"tasklist_id": "L1", "task_id": "t2"}),
    ]

    async def run() -> list[Any]:
        async with Client(mcp) as client:
            return [(await client.call_tool(name, args)).structured_content for name, args in calls]

    results = asyncio.run(run())

    base = "https://tasks.googleapis.com/tasks/v1"
    expected = [
        ("GET", f"{base}/users/@me/lists", {"maxResults": ["100"]}, None),
        (
            "GET",
            f"{base}/lists/L1/tasks",
            {"showCompleted": ["true"], "showHidden": ["true"], "maxResults": ["25"]},
            None,
        ),
        ("GET", f"{base}/lists/L1/tasks/t1", {}, None),
        (
            "POST",
            f"{base}/lists/L1/tasks",
            {},
            {"title": "Call bank", "due": "2026-10-07T00:00:00.000Z"},
        ),
        (
            "PATCH",
            f"{base}/lists/L1/tasks/t2",
            {},
            {"title": "Call bank today", "notes": "ask about fees"},
        ),
        ("PATCH", f"{base}/lists/L1/tasks/t2", {}, {"status": "completed"}),
        ("DELETE", f"{base}/lists/L1/tasks/t2", {}, None),
    ]
    assert len(ctx.requests) == len(expected)
    for request, (method, url, query, body) in zip(ctx.requests, expected, strict=True):
        assert request.method == method
        assert request.uri.split("?")[0].replace("%40", "@") == url
        assert _query(request.uri) == query
        assert request.body == body

    assert results[0] == {"tasklists": [{"id": "L1", "title": "Inbox"}], "next_page_token": "p2"}
    assert results[2] == {"id": "t1", "title": "Pay rent", "status": "needsAction"}
    assert results[5] == {
        "id": "t2",
        "status": "completed",
        "completed": "2026-10-06T18:00:00.000Z",
    }
    assert results[6] == {"deleted": True, "task_id": "t2", "tasklist_id": "L1"}


def test_hidden_tools_cannot_be_called(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, WRITE_ENV)
    ctx = FakeContext()
    mcp = _server(ctx)

    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(
                "tasks_delete_task", {"task_id": "t1"}, raise_on_error=False
            )

    result = asyncio.run(run())
    assert result.is_error
    assert ctx.requests == []


def test_invalid_input_is_a_tool_error_with_no_google_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, WRITE_ENV)
    ctx = FakeContext()
    mcp = _server(ctx)

    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(
                "tasks_create_task", {"title": "x", "due": "next week"}, raise_on_error=False
            )

    result = asyncio.run(run())
    assert result.is_error
    assert "due must be YYYY-MM-DD" in result.content[0].text
    assert ctx.requests == []


def test_google_error_surfaces_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, READ_ONLY_ENV)
    ctx = FakeContext().respond({"error": {"code": 404, "message": "Not Found"}}, status=404)
    mcp = _server(ctx)

    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(
                "tasks_get_task", {"task_id": "gone"}, raise_on_error=False
            )

    result = asyncio.run(run())
    assert result.is_error
    assert "404" in result.content[0].text
