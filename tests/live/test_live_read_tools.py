"""Read-only live smoke: one read tool per default-enabled product.

Runs the real MCP server in-process (fastmcp in-memory client, no network
listener) with the locally stored credentials and calls one read tool per
product. Writes and destructive tools are switched off in the server settings
and every called tool must carry the MCP read-only hint. Nothing is created, sent,
or deleted.

Run: ``GOOGLEMCP_LIVE=1 uv run pytest tests/live -v -rs``

With GOOGLEMCP_LIVE=1 the run fails (never skips) when credentials are
missing or an enabled product's read tool is not exposed, and the final test
requires at least one product read to succeed. The only allowed skips are:

- the product is not enabled in ``GOOGLEMCP_PRODUCTS``;
- Docs, Sheets, Slides, or Forms: no file of that type. These APIs have no
  list call, so the test reads ``GOOGLEMCP_LIVE_<PRODUCT>_ID`` (for example
  GOOGLEMCP_LIVE_DOCS_ID) or the first match from a read-only Drive search;
- YouTube Analytics: the account has no YouTube channel.

Core modules are imported inside the fixture so this file imports cleanly
for the offline guards in tests/test_acceptance_scripts.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Any

import pytest

# Skipped unless GOOGLEMCP_LIVE=1, so the default ``uv run pytest`` stays offline.
# This lives here, not in a tests/live/conftest.py, because a second conftest
# module would shadow ``from conftest import ...`` in the root test suite.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GOOGLEMCP_LIVE") != "1", reason="live Google tests need GOOGLEMCP_LIVE=1"
    ),
]


@dataclass(frozen=True)
class ReadCall:
    """One read-only tool call. ``drive_mime`` and ``id_param`` describe how to
    find an existing file id for tools that need one."""

    product: str
    tool: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    drive_mime: str | None = None
    id_param: str | None = None


LIVE_READ_CALLS: tuple[ReadCall, ...] = (
    ReadCall("tasks", "tasks_list_tasklists", {"max_results": 1}),
    ReadCall("gmail", "gmail_list_labels"),
    ReadCall("calendar", "calendar_list_calendars", {"max_results": 1}),
    ReadCall("drive", "drive_search_files", {"max_results": 1}),
    ReadCall(
        "docs",
        "docs_get_document",
        {"max_chars": 200},
        "application/vnd.google-apps.document",
        "document_id",
    ),
    ReadCall(
        "sheets",
        "sheets_get_spreadsheet",
        {},
        "application/vnd.google-apps.spreadsheet",
        "spreadsheet_id",
    ),
    ReadCall(
        "slides",
        "slides_get_presentation",
        {},
        "application/vnd.google-apps.presentation",
        "presentation_id",
    ),
    ReadCall("forms", "forms_get_form", {}, "application/vnd.google-apps.form", "form_id"),
    ReadCall("people", "people_list_connections", {"max_results": 1}),
    ReadCall("driveactivity", "driveactivity_query_activity", {"max_results": 1}),
    ReadCall("youtube", "youtube_list_my_channels"),
    ReadCall("youtube_analytics", "youtube_analytics_query_report", {"metrics": ["views"]}),
    ReadCall("analytics", "analytics_list_account_summaries", {"page_size": 1}),
    ReadCall("searchconsole", "searchconsole_list_sites"),
    ReadCall("photos", "photos_list_app_albums", {"page_size": 1}),
)


@dataclass
class LiveServer:
    mcp: Any
    enabled: set[str]
    tools: dict[str, Any]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(_call_tool(self.mcp, name, arguments))


def _read_only(tool: Any) -> bool:
    """MCP SDK v2 names the hint read_only_hint; older SDKs use readOnlyHint."""
    hints = getattr(tool, "annotations", None)
    if hints is None:
        return False
    value = getattr(hints, "read_only_hint", None)
    if value is None:
        value = getattr(hints, "readOnlyHint", None)
    return bool(value)


async def _call_tool(mcp: Any, name: str, arguments: dict[str, Any]) -> Any:
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    return result.structured_content if result.structured_content is not None else result.data


async def _list_tools(mcp: Any) -> dict[str, Any]:
    from fastmcp import Client

    async with Client(mcp) as client:
        return {tool.name: tool for tool in await client.list_tools()}


# Products whose read call returned a result in this run.
SUCCEEDED: list[str] = []


@pytest.fixture(scope="module")
def live_server() -> LiveServer:
    from googlemcp import auth, config, secretstore, server

    # Reads only, whatever the environment says.
    settings = config.load_settings(write_products=[], allow_destructive=False)
    store = secretstore.open_store(settings)
    report = auth.status(store, [], check=False)
    if not (report.client_configured and report.refresh_token_present):
        pytest.fail(
            f"no stored Google credentials in the {report.backend} store; "
            "run `uv run googlemcp auth login` first"
        )
    mcp = server.build_server(settings, store)
    tools = asyncio.run(_list_tools(mcp))
    exposed_writes = sorted(name for name, tool in tools.items() if not _read_only(tool))
    assert not exposed_writes, f"write tools exposed in read-only live run: {exposed_writes}"
    return LiveServer(mcp=mcp, enabled=set(settings.enabled_products()), tools=tools)


def _find_file_id(server: LiveServer, call: ReadCall) -> str:
    env_name = f"GOOGLEMCP_LIVE_{call.product.upper()}_ID"
    if os.environ.get(env_name):
        return os.environ[env_name]
    if "drive_search_files" not in server.tools:
        pytest.skip(f"set {env_name}; Drive is not enabled to look up a {call.product} file")
    found = server.call("drive_search_files", {"mime_type": call.drive_mime, "max_results": 1})
    files = (found or {}).get("files") or []
    if not files or not files[0].get("id"):
        pytest.skip(f"no {call.product} file in Drive; set {env_name} to an existing id")
    return str(files[0]["id"])


def _arguments(server: LiveServer, call: ReadCall) -> dict[str, Any]:
    arguments = dict(call.kwargs)
    if call.id_param:
        arguments[call.id_param] = _find_file_id(server, call)
    if call.product == "youtube_analytics":
        channels: Any = {}
        if "youtube_list_my_channels" in server.tools:
            channels = server.call("youtube_list_my_channels", {})
        if not (channels or {}).get("channels"):
            pytest.skip("YouTube Analytics needs a YouTube channel on the authorized account")
        today = dt.date.today()
        arguments["start_date"] = (today - dt.timedelta(days=28)).isoformat()
        arguments["end_date"] = today.isoformat()
    return arguments


@pytest.mark.parametrize("call", LIVE_READ_CALLS, ids=lambda c: c.product)
def test_read_tool(live_server: LiveServer, call: ReadCall) -> None:
    if call.product not in live_server.enabled:
        pytest.skip(f"{call.product} is not enabled (GOOGLEMCP_PRODUCTS)")
    tool = live_server.tools.get(call.tool)
    assert tool is not None, f"{call.product} is enabled but {call.tool} is not exposed"
    assert _read_only(tool), f"{call.tool} is not read-only"

    result = live_server.call(call.tool, _arguments(live_server, call))

    assert isinstance(result, dict), f"{call.tool} returned {type(result).__name__}"
    SUCCEEDED.append(call.product)


def test_at_least_one_product_read_succeeded(live_server: LiveServer) -> None:
    """Runs after the per-product tests (file order): an all-skipped run is not acceptance."""
    assert SUCCEEDED, "no product read succeeded; every product test was skipped"
