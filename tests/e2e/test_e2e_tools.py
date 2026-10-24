"""One read tool per default-enabled product, called end to end through MCP.

Each canned Google response carries ``SENTINEL`` in a field the tool returns,
so a pass proves the request reached the right API with the stored account's access
token and the response came back as structured MCP output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from e2e_harness import (
    ACCESS_TOKEN,
    FakeGoogle,
    all_adapters,
    assert_no_secrets,
    call_tool,
    make_server,
    require_adapter,
    result_text,
)

from googlemcp.catalog import load_catalog
from googlemcp.config import load_settings

SENTINEL = "e2e-sentinel-7f3a"


@dataclass(frozen=True)
class Smoke:
    tool: str
    args: dict[str, Any]
    method: str
    path: str  # regex matched against the request path
    response: dict[str, Any]


SMOKE: dict[str, Smoke] = {
    "tasks": Smoke(
        "tasks_list_tasklists",
        {},
        "GET",
        r"/tasks/v1/users/@me/lists$",
        {"items": [{"id": SENTINEL, "title": "List"}]},
    ),
    "gmail": Smoke(
        "gmail_list_labels",
        {},
        "GET",
        r"/gmail/v1/users/me/labels$",
        {"labels": [{"id": SENTINEL, "name": "INBOX", "type": "system"}]},
    ),
    "calendar": Smoke(
        "calendar_list_calendars",
        {},
        "GET",
        r"/calendar/v3/users/me/calendarList$",
        {"items": [{"id": SENTINEL, "summary": "Primary"}], "nextPageToken": SENTINEL},
    ),
    "drive": Smoke(
        "drive_search_files",
        {},
        "GET",
        r"/drive/v3/files$",
        {"files": [], "nextPageToken": SENTINEL},
    ),
    "docs": Smoke(
        "docs_get_document",
        {"document_id": "doc1"},
        "GET",
        r"/v1/documents/doc1$",
        {"documentId": "doc1", "title": SENTINEL, "body": {"content": []}},
    ),
    "sheets": Smoke(
        "sheets_get_spreadsheet",
        {"spreadsheet_id": "sheet1"},
        "GET",
        r"/v4/spreadsheets/sheet1$",
        {"spreadsheetId": "sheet1", "properties": {"title": SENTINEL}, "sheets": []},
    ),
    "slides": Smoke(
        "slides_get_presentation",
        {"presentation_id": "pres1"},
        "GET",
        r"/v1/presentations/pres1$",
        {"presentationId": "pres1", "title": SENTINEL, "slides": []},
    ),
    "forms": Smoke(
        "forms_get_form",
        {"form_id": "form1"},
        "GET",
        r"/v1/forms/form1$",
        {"formId": "form1", "info": {"title": SENTINEL}, "items": []},
    ),
    "people": Smoke(
        "people_list_connections",
        {},
        "GET",
        r"/v1/people/me/connections$",
        {"connections": [], "nextPageToken": SENTINEL},
    ),
    "driveactivity": Smoke(
        "driveactivity_query_activity",
        {},
        "POST",
        r"/v2/activity:query$",
        {"activities": [], "nextPageToken": SENTINEL},
    ),
    "youtube": Smoke(
        "youtube_list_my_channels",
        {},
        "GET",
        r"/youtube/v3/channels$",
        {"items": [{"id": SENTINEL, "snippet": {"title": "Me"}}]},
    ),
    "youtube_analytics": Smoke(
        "youtube_analytics_query_report",
        {"metrics": ["views"], "start_date": "2026-09-01", "end_date": "2026-09-30"},
        "GET",
        r"/v2/reports$",
        {"columnHeaders": [{"name": "views"}], "rows": [[SENTINEL]]},
    ),
    "analytics": Smoke(
        "analytics_list_account_summaries",
        {},
        "GET",
        r"/v1beta/accountSummaries$",
        {"accountSummaries": [], "nextPageToken": SENTINEL},
    ),
    "searchconsole": Smoke(
        "searchconsole_list_sites",
        {},
        "GET",
        r"/webmasters/v3/sites$",
        {"siteEntry": [{"siteUrl": SENTINEL, "permissionLevel": "siteOwner"}]},
    ),
    "photos": Smoke(
        "photos_list_app_albums",
        {},
        "GET",
        r"^/v1/albums$",
        {"albums": [{"id": SENTINEL, "title": "App album"}]},
    ),
}

ADAPTERS = all_adapters()
CATALOG = load_catalog()
DEFAULT_ON = sorted(k for k in ADAPTERS if CATALOG[k].default_enabled)


def test_every_default_enabled_adapter_has_a_smoke_case() -> None:
    missing = [k for k in DEFAULT_ON if k not in SMOKE]
    assert not missing, f"add an end-to-end smoke case for: {missing}"


@pytest.mark.parametrize("product", sorted(SMOKE))
def test_read_tool_end_to_end(product: str) -> None:
    require_adapter(product)
    case = SMOKE[product]
    google = FakeGoogle().on(case.method, case.path, case.response)
    mcp = make_server(load_settings(), google)

    result = call_tool(mcp, case.tool, case.args)

    assert not result.is_error, result_text(result)
    assert isinstance(result.structured_content, dict)
    assert SENTINEL in json.dumps(result.structured_content)
    assert google.unmatched == [], [r.url for r in google.unmatched]
    assert len(google.requests) == 1
    sent = google.requests[0]
    assert sent.host.endswith("googleapis.com"), sent.host
    assert sent.headers.get("authorization") == f"Bearer {ACCESS_TOKEN}"
    assert_no_secrets([result_text(result), json.dumps(result.structured_content)])
