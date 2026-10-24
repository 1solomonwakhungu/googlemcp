"""YouTube Analytics API v2 adapter (read-only channel reports).

Uses ``reports.query``. Metric and dimension names follow
developers.google.com/youtube/analytics/metrics and /dimensions; Google
rejects unsupported combinations with a 400 that is surfaced to the caller.
"""

from __future__ import annotations

import re

from googlemcp.services._reporting import clamp_rows, date_range, field_names
from googlemcp.services.base import JSON, GoogleMCPError, ServiceContext, ServiceSpec, ToolRegistry

SPEC = ServiceSpec(
    key="youtube_analytics",
    title="YouTube Analytics",
    api_services=("youtubeanalytics.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/yt-analytics.readonly",),
)

_CHANNEL_IDS = re.compile(r"^channel==(MINE|UC[A-Za-z0-9_-]{22})$")
_SORT = re.compile(r"^-?[A-Za-z][A-Za-z0-9_]*(,-?[A-Za-z][A-Za-z0-9_]*)*$")
_MAX_METRICS = 20
_MAX_DIMENSIONS = 5
_MAX_FILTER_LEN = 500


class YouTubeAnalyticsAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("youtube_analytics_query_report", idempotent=True)
        def query_report(
            metrics: list[str],
            start_date: str,
            end_date: str,
            dimensions: list[str] | None = None,
            filters: str | None = None,
            sort: str | None = None,
            max_results: int = 100,
            channel: str = "channel==MINE",
        ) -> JSON:
            """Query a YouTube Analytics report for the user's channel.

            metrics: e.g. ["views", "estimatedMinutesWatched", "likes"].
            dimensions: e.g. ["day"] or ["video"] (top videos need sort "-views").
            Dates are YYYY-MM-DD and inclusive. filters uses the API syntax,
            e.g. "video==VIDEO_ID". channel defaults to "channel==MINE".
            Returns column names and rows (at most 1000)."""
            start, end = date_range(start_date, end_date)
            metric_names = field_names(
                metrics, field="metrics", max_count=_MAX_METRICS, required=True
            )
            dim_names = field_names(
                dimensions or [], field="dimensions", max_count=_MAX_DIMENSIONS, required=False
            )
            if not _CHANNEL_IDS.match(channel):
                raise GoogleMCPError("channel must be 'channel==MINE' or 'channel==<channel ID>'")
            if sort is not None and not _SORT.match(sort):
                raise GoogleMCPError("sort must be comma-separated names, '-' prefix to descend")
            if filters is not None and len(filters) > _MAX_FILTER_LEN:
                raise GoogleMCPError(f"filters is limited to {_MAX_FILTER_LEN} characters")
            params: JSON = {
                "ids": channel,
                "startDate": start,
                "endDate": end,
                "metrics": ",".join(metric_names),
                "maxResults": clamp_rows(max_results),
            }
            if dim_names:
                params["dimensions"] = ",".join(dim_names)
            if filters:
                params["filters"] = filters
            if sort:
                params["sort"] = sort
            svc = ctx.client("youtubeAnalytics", "v2")
            resp = ctx.execute(svc.reports().query(**params))
            return {
                "columns": [h.get("name") for h in resp.get("columnHeaders", [])],
                "rows": resp.get("rows", []),
            }


ADAPTER = YouTubeAnalyticsAdapter()
