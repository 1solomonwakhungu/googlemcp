import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.searchconsole import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def test_read_only() -> None:
    assert not any(t.write for t in registry_for(ADAPTER, FakeContext()).tools)
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/webmasters.readonly",)


def test_list_sites() -> None:
    ctx = FakeContext().respond(
        {"siteEntry": [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]}
    )
    out = call(registry_for(ADAPTER, ctx), "searchconsole_list_sites")
    assert out == {"sites": [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]}
    assert "/webmasters/v3/sites" in ctx.requests[0].uri


def test_query_search_analytics_request() -> None:
    ctx = FakeContext().respond(
        {
            "rows": [{"keys": ["mcp"], "clicks": 3, "impressions": 10, "ctr": 0.3, "position": 2}],
            "responseAggregationType": "byProperty",
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "searchconsole_query_search_analytics",
        site_url="https://example.com/",
        start_date="2026-09-01",
        end_date="2026-09-30",
        dimensions=["query"],
        filters=[{"dimension": "query", "operator": "contains", "expression": "mcp"}],
        row_limit=50_000,
    )
    assert out == {
        "dimensions": ["query"],
        "rows": [{"keys": ["mcp"], "clicks": 3, "impressions": 10, "ctr": 0.3, "position": 2}],
        "aggregation": "byProperty",
    }
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "/sites/https%3A%2F%2Fexample.com%2F/searchAnalytics/query" in req.uri
    assert req.body == {
        "startDate": "2026-09-01",
        "endDate": "2026-09-30",
        "dimensions": ["query"],
        "type": "web",
        "rowLimit": 1000,
        "startRow": 0,
        "dimensionFilterGroups": [
            {
                "groupType": "and",
                "filters": [{"dimension": "query", "operator": "contains", "expression": "mcp"}],
            }
        ],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"site_url": "example.com"},
        {"dimensions": ["keyword"]},
        {"search_type": "shopping"},
        {"start_date": "2026-10-01"},
        {"start_row": -5},
        {"filters": [{"dimension": "date", "operator": "equals", "expression": "x"}]},
        {"filters": [{"dimension": "query", "operator": "like", "expression": "x"}]},
        {"filters": [{"dimension": "query", "operator": "equals", "expression": ""}]},
        {"filters": [{"dimension": "query", "expression": "x"}] * 11},
    ],
)
def test_query_validation(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {
        "site_url": "sc-domain:example.com",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        **kwargs,
    }
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "searchconsole_query_search_analytics", **args)
    assert ctx.requests == []


def test_inspect_url() -> None:
    ctx = FakeContext().respond(
        {
            "inspectionResult": {
                "inspectionResultLink": "https://search.google.com/x",
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "referringUrls": ["https://drop.me"],
                },
                "richResultsResult": {"verdict": "PASS", "detectedItems": []},
            }
        }
    )
    reg = registry_for(ADAPTER, ctx)
    out = call(
        reg,
        "searchconsole_inspect_url",
        site_url="sc-domain:example.com",
        url="https://example.com/a",
    )
    assert out == {
        "link": "https://search.google.com/x",
        "index_status": {"verdict": "PASS", "coverageState": "Submitted and indexed"},
        "rich_results": "PASS",
    }
    req = ctx.requests[0]
    assert "/v1/urlInspection/index:inspect" in req.uri
    assert req.body == {
        "inspectionUrl": "https://example.com/a",
        "siteUrl": "sc-domain:example.com",
        "languageCode": "en-US",
    }
    with pytest.raises(GoogleMCPError):
        call(reg, "searchconsole_inspect_url", site_url="sc-domain:example.com", url="/a")


def test_upstream_error() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError):
        call(registry_for(ADAPTER, ctx), "searchconsole_list_sites")
