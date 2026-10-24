from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.youtube_analytics import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for

TOOL = "youtube_analytics_query_report"


def test_query_report_builds_request_and_shapes_rows() -> None:
    ctx = FakeContext().respond(
        {
            "kind": "youtubeAnalytics#resultTable",
            "columnHeaders": [
                {"name": "day", "columnType": "DIMENSION", "dataType": "STRING"},
                {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
            ],
            "rows": [["2026-09-01", 12], ["2026-09-02", 7]],
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        TOOL,
        metrics=["views"],
        dimensions=["day"],
        start_date="2026-09-01",
        end_date="2026-09-02",
        sort="day",
        max_results=5000,
    )
    assert out == {"columns": ["day", "views"], "rows": [["2026-09-01", 12], ["2026-09-02", 7]]}
    req = ctx.requests[0]
    assert req.method == "GET"
    assert "/v2/reports" in req.uri
    q = parse_qs(urlparse(req.uri).query)
    assert q["ids"] == ["channel==MINE"]
    assert q["metrics"] == ["views"]
    assert q["dimensions"] == ["day"]
    assert q["maxResults"] == ["1000"]
    assert q["startDate"] == ["2026-09-01"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metrics": [], "start_date": "2026-01-01", "end_date": "2026-01-02"},
        {"metrics": ["views"], "start_date": "2026-01-05", "end_date": "2026-01-02"},
        {"metrics": ["views"], "start_date": "2026-02-30", "end_date": "2026-03-02"},
        {"metrics": ["views"], "start_date": "yesterday", "end_date": "2026-01-02"},
        {"metrics": ["views;drop"], "start_date": "2026-01-01", "end_date": "2026-01-02"},
        {"metrics": ["views", "views"], "start_date": "2026-01-01", "end_date": "2026-01-02"},
        {
            "metrics": ["views"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "channel": "contentOwner==X",
        },
        {
            "metrics": ["views"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "sort": "views desc",
        },
        {
            "metrics": ["views"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "filters": "x" * 501,
        },
    ],
)
def test_invalid_input_is_rejected_before_any_request(kwargs: dict[str, object]) -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), TOOL, **kwargs)
    assert ctx.requests == []


def test_upstream_error() -> None:
    ctx = FakeContext().respond({"error": {"code": 400}}, status=400)
    with pytest.raises(UpstreamError):
        call(
            registry_for(ADAPTER, ctx),
            TOOL,
            metrics=["views"],
            start_date="2026-01-01",
            end_date="2026-01-02",
        )


def test_read_only() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    assert not any(t.write for t in reg.tools)
    assert ADAPTER.spec.write_scopes == ()
