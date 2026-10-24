from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.catalog import load_catalog
from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.youtube_reporting import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def test_catalog_keeps_reporting_off_by_default() -> None:
    assert load_catalog()["youtube_reporting"].default_enabled is False


def test_only_create_job_writes() -> None:
    tools = registry_for(ADAPTER, FakeContext()).tools
    assert {t.name for t in tools if t.write} == {"youtube_reporting_create_job"}
    assert not any(t.destructive for t in tools)


def test_list_report_types() -> None:
    ctx = FakeContext().respond(
        {"reportTypes": [{"id": "channel_basic_a2", "name": "Basic", "deprecateTime": "x"}]}
    )
    out = call(registry_for(ADAPTER, ctx), "youtube_reporting_list_report_types", page_size=999)
    assert out == {
        "report_types": [{"id": "channel_basic_a2", "name": "Basic"}],
        "next_page_token": None,
    }
    assert "/v1/reportTypes" in ctx.requests[0].uri
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["pageSize"] == ["100"]


def test_list_jobs() -> None:
    ctx = FakeContext().respond({"jobs": [{"id": "j1", "reportTypeId": "r", "name": "n"}]})
    out = call(registry_for(ADAPTER, ctx), "youtube_reporting_list_jobs")
    assert out["jobs"] == [{"id": "j1", "reportTypeId": "r", "name": "n"}]


def test_list_reports_for_job() -> None:
    ctx = FakeContext().respond(
        {"reports": [{"id": "r1", "jobId": "j1", "downloadUrl": "https://example/x"}]}
    )
    reg = registry_for(ADAPTER, ctx)
    out = call(
        reg, "youtube_reporting_list_reports", job_id="j1", created_after="2026-09-01T00:00:00Z"
    )
    assert out["reports"][0]["downloadUrl"] == "https://example/x"
    req = ctx.requests[0]
    assert "/v1/jobs/j1/reports" in req.uri
    assert parse_qs(urlparse(req.uri).query)["createdAfter"] == ["2026-09-01T00:00:00Z"]
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_reporting_list_reports", job_id="")


def test_create_job_sends_body() -> None:
    ctx = FakeContext().respond({"id": "j2", "reportTypeId": "channel_basic_a2", "name": "daily"})
    out = call(
        registry_for(ADAPTER, ctx),
        "youtube_reporting_create_job",
        report_type_id="channel_basic_a2",
        job_name="daily",
    )
    assert out["id"] == "j2"
    assert ctx.requests[0].method == "POST"
    assert ctx.requests[0].body == {"reportTypeId": "channel_basic_a2", "name": "daily"}


@pytest.mark.parametrize(
    ("report_type_id", "job_name"), [("Channel Basic", "x"), ("channel_basic_a2", " "), ("", "x")]
)
def test_create_job_validation(report_type_id: str, job_name: str) -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(
            registry_for(ADAPTER, ctx),
            "youtube_reporting_create_job",
            report_type_id=report_type_id,
            job_name=job_name,
        )
    assert ctx.requests == []


def test_upstream_error() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError):
        call(registry_for(ADAPTER, ctx), "youtube_reporting_list_jobs")
