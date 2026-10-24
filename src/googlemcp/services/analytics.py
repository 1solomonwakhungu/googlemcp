"""Google Analytics (GA4) adapter: Admin API v1beta and Data API v1beta.

Both APIs ship static discovery documents in google-api-python-client
(``analyticsadmin`` v1beta, ``analyticsdata`` v1beta), so no REST fallback is
needed. Universal Analytics is retired and not covered.
Limits follow developers.google.com/analytics/devguides/reporting/data/v1:
at most 9 dimensions, 10 metrics, and 4 date ranges per report.
"""

from __future__ import annotations

import re

from googlemcp.services._reporting import clamp_rows, date_range, field_names, iso_date
from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="analytics",
    title="Google Analytics",
    api_services=("analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"),
    read_scopes=("https://www.googleapis.com/auth/analytics.readonly",),
)

_MAX_DIMENSIONS = 9
_MAX_METRICS = 10
_MAX_DATE_RANGES = 4
_PROPERTY = re.compile(r"^(properties/)?(\d{1,20})$")
_ACCOUNT = re.compile(r"^(accounts/)?(\d{1,20})$")
# GA4 accepts YYYY-MM-DD, "today", "yesterday", or "NdaysAgo".
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GA_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}|today|yesterday|\d{1,4}daysAgo)$")


def _property_name(value: str) -> str:
    match = _PROPERTY.match(value.strip())
    if not match:
        raise GoogleMCPError("property_id must be a numeric ID or 'properties/<ID>'")
    return f"properties/{match.group(2)}"


def _date_ranges(ranges: list[dict[str, str]]) -> list[JSON]:
    if not ranges:
        raise GoogleMCPError("date_ranges must contain at least one range")
    if len(ranges) > _MAX_DATE_RANGES:
        raise GoogleMCPError(f"date_ranges accepts at most {_MAX_DATE_RANGES} ranges")
    out: list[JSON] = []
    for r in ranges:
        start, end = r.get("start_date", ""), r.get("end_date", "")
        if not _GA_DATE.match(start) or not _GA_DATE.match(end):
            raise GoogleMCPError(
                "each date range needs start_date and end_date as YYYY-MM-DD, "
                "'today', 'yesterday', or 'NdaysAgo'"
            )
        # Relative forms are resolved by Google; calendar dates are checked
        # here, each one on its own, and ordered when both are calendar dates.
        if _is_iso(start):
            iso_date(start, "start_date")
        if _is_iso(end):
            iso_date(end, "end_date")
        if _is_iso(start) and _is_iso(end):
            date_range(start, end)
        out.append({"startDate": start, "endDate": end})
    return out


def _is_iso(value: str) -> bool:
    return bool(_ISO_DATE.match(value))


class AnalyticsAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("analytics_list_account_summaries", idempotent=True)
        def list_account_summaries(page_size: int = 50, page_token: str | None = None) -> JSON:
            """List GA4 accounts and their properties visible to the user."""
            svc = ctx.client("analyticsadmin", "v1beta")
            resp = ctx.execute(
                svc.accountSummaries().list(
                    pageSize=clamp_page_size(page_size), pageToken=page_token
                )
            )
            return {
                "accounts": [
                    {
                        **pick(a, ("account", "displayName")),
                        "properties": [
                            pick(p, ("property", "displayName", "propertyType"))
                            for p in a.get("propertySummaries", [])
                        ],
                    }
                    for a in resp.get("accountSummaries", [])
                ],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("analytics_list_properties", idempotent=True)
        def list_properties(
            account: str, page_size: int = 50, page_token: str | None = None
        ) -> JSON:
            """List GA4 properties under an account ('accounts/<ID>' or the numeric ID)."""
            match = _ACCOUNT.match(account.strip())
            if not match:
                raise GoogleMCPError("account must be a numeric ID or 'accounts/<ID>'")
            svc = ctx.client("analyticsadmin", "v1beta")
            resp = ctx.execute(
                svc.properties().list(
                    filter=f"parent:accounts/{match.group(2)}",
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                )
            )
            fields = ("name", "displayName", "timeZone", "currencyCode", "createTime")
            return {
                "properties": [pick(p, fields) for p in resp.get("properties", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("analytics_run_report", idempotent=True)
        def run_report(
            property_id: str,
            metrics: list[str],
            date_ranges: list[dict[str, str]],
            dimensions: list[str] | None = None,
            limit: int = 100,
            offset: int = 0,
            order_by_metric: str | None = None,
            descending: bool = True,
        ) -> JSON:
            """Run a GA4 Data API report.

            property_id: numeric ID or 'properties/<ID>'.
            metrics: e.g. ["activeUsers", "sessions"] (max 10).
            dimensions: e.g. ["date", "country"] (max 9).
            date_ranges: [{"start_date": "28daysAgo", "end_date": "today"}] (max 4).
            Returns headers and rows (at most 1000 per call; page with offset)."""
            name = _property_name(property_id)
            metric_names = field_names(
                metrics, field="metrics", max_count=_MAX_METRICS, required=True
            )
            dim_names = field_names(
                dimensions or [], field="dimensions", max_count=_MAX_DIMENSIONS, required=False
            )
            if offset < 0:
                raise GoogleMCPError("offset must be 0 or greater")
            body: JSON = {
                "dateRanges": _date_ranges(date_ranges),
                "metrics": [{"name": m} for m in metric_names],
                "dimensions": [{"name": d} for d in dim_names],
                "limit": str(clamp_rows(limit)),
                "offset": str(offset),
            }
            if order_by_metric is not None:
                if order_by_metric not in metric_names:
                    raise GoogleMCPError("order_by_metric must be one of the requested metrics")
                body["orderBys"] = [{"metric": {"metricName": order_by_metric}, "desc": descending}]
            svc = ctx.client("analyticsdata", "v1beta")
            resp = ctx.execute(svc.properties().runReport(property=name, body=body))
            return {
                "dimension_headers": [h.get("name") for h in resp.get("dimensionHeaders", [])],
                "metric_headers": [h.get("name") for h in resp.get("metricHeaders", [])],
                "rows": [
                    [v.get("value") for v in row.get("dimensionValues", [])]
                    + [v.get("value") for v in row.get("metricValues", [])]
                    for row in resp.get("rows", [])
                ],
                "row_count": resp.get("rowCount", 0),
            }


ADAPTER = AnalyticsAdapter()
