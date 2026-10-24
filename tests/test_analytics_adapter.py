from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.analytics import ADAPTER
from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.testing import FakeContext, call, registry_for

RANGE = [{"start_date": "28daysAgo", "end_date": "today"}]


def test_read_only() -> None:
    assert not any(t.write for t in registry_for(ADAPTER, FakeContext()).tools)
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/analytics.readonly",)


def test_list_account_summaries() -> None:
    ctx = FakeContext().respond(
        {
            "accountSummaries": [
                {
                    "name": "accountSummaries/1",
                    "account": "accounts/1",
                    "displayName": "Acct",
                    "propertySummaries": [
                        {"property": "properties/9", "displayName": "Site", "parent": "accounts/1"}
                    ],
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "analytics_list_account_summaries")
    assert out == {
        "accounts": [
            {
                "account": "accounts/1",
                "displayName": "Acct",
                "properties": [{"property": "properties/9", "displayName": "Site"}],
            }
        ],
        "next_page_token": None,
    }
    assert "analyticsadmin.googleapis.com/v1beta/accountSummaries" in ctx.requests[0].uri


def test_list_properties_builds_filter() -> None:
    ctx = FakeContext().respond({"properties": [{"name": "properties/9", "displayName": "S"}]})
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "analytics_list_properties", account="1234")
    assert out["properties"] == [{"name": "properties/9", "displayName": "S"}]
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["filter"] == ["parent:accounts/1234"]
    with pytest.raises(GoogleMCPError):
        call(reg, "analytics_list_properties", account="accounts/abc")


def test_run_report_body_and_shaping() -> None:
    ctx = FakeContext().respond(
        {
            "dimensionHeaders": [{"name": "country"}],
            "metricHeaders": [{"name": "activeUsers", "type": "TYPE_INTEGER"}],
            "rows": [
                {"dimensionValues": [{"value": "KE"}], "metricValues": [{"value": "42"}]},
                {"dimensionValues": [{"value": "US"}], "metricValues": [{"value": "7"}]},
            ],
            "rowCount": 2,
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "analytics_run_report",
        property_id="properties/9",
        metrics=["activeUsers"],
        dimensions=["country"],
        date_ranges=RANGE,
        limit=1_000_000,
        order_by_metric="activeUsers",
    )
    assert out == {
        "dimension_headers": ["country"],
        "metric_headers": ["activeUsers"],
        "rows": [["KE", "42"], ["US", "7"]],
        "row_count": 2,
    }
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "analyticsdata.googleapis.com/v1beta/properties/9:runReport" in req.uri
    assert req.body == {
        "dateRanges": [{"startDate": "28daysAgo", "endDate": "today"}],
        "metrics": [{"name": "activeUsers"}],
        "dimensions": [{"name": "country"}],
        "limit": "1000",
        "offset": "0",
        "orderBys": [{"metric": {"metricName": "activeUsers"}, "desc": True}],
    }


def test_run_report_accepts_numeric_property() -> None:
    ctx = FakeContext().respond({})
    out = call(
        registry_for(ADAPTER, ctx),
        "analytics_run_report",
        property_id="9",
        metrics=["sessions"],
        date_ranges=[{"start_date": "2026-09-01", "end_date": "2026-09-30"}],
    )
    assert out == {"dimension_headers": [], "metric_headers": [], "rows": [], "row_count": 0}
    assert "/v1beta/properties/9:runReport" in ctx.requests[0].uri


@pytest.mark.parametrize(
    "kwargs",
    [
        {"property_id": "abc"},
        {"metrics": []},
        {"metrics": [f"m{i}" for i in range(11)]},
        {"dimensions": [f"d{i}" for i in range(10)]},
        {"date_ranges": []},
        {"date_ranges": RANGE * 5},
        {"date_ranges": [{"start_date": "last week", "end_date": "today"}]},
        {"date_ranges": [{"start_date": "2026-09-30", "end_date": "2026-09-01"}]},
        {"date_ranges": [{"start_date": "2026-13-01", "end_date": "2026-13-02"}]},
        # Regression: an invalid calendar date beside a relative date.
        {"date_ranges": [{"start_date": "2026-13-01", "end_date": "today"}]},
        {"date_ranges": [{"start_date": "7daysAgo", "end_date": "2026-02-30"}]},
        {"date_ranges": [{"start_date": "yesterday", "end_date": "2026-00-10"}]},
        {"offset": -1},
        {"order_by_metric": "sessions"},
    ],
)
def test_run_report_validation(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {
        "property_id": "9",
        "metrics": ["activeUsers"],
        "date_ranges": RANGE,
        **kwargs,
    }
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "analytics_run_report", **args)
    assert ctx.requests == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-01", "today"),
        ("30daysAgo", "2026-09-30"),
        ("yesterday", "today"),
        ("2024-02-29", "2024-02-29"),
    ],
)
def test_run_report_accepts_mixed_valid_dates(start: str, end: str) -> None:
    ctx = FakeContext().respond({})
    call(
        registry_for(ADAPTER, ctx),
        "analytics_run_report",
        property_id="9",
        metrics=["sessions"],
        date_ranges=[{"start_date": start, "end_date": end}],
    )
    assert ctx.requests[0].body["dateRanges"] == [{"startDate": start, "endDate": end}]


def test_upstream_error() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "analytics_list_account_summaries")
    assert info.value.status == 403
