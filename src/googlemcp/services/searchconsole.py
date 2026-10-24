"""Google Search Console adapter (Search Console API v1, read-only).

Search analytics rows are capped at 1000 per call (the API allows 25,000).
URL inspection is limited by Google to 2,000 calls per day and 600 per minute
per property (developers.google.com/webmaster-tools/limits).
"""

from __future__ import annotations

from googlemcp.services._reporting import clamp_rows, date_range, field_names
from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    pick,
)

SPEC = ServiceSpec(
    key="searchconsole",
    title="Search Console",
    api_services=("searchconsole.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/webmasters.readonly",),
)

_DIMENSIONS = frozenset({"date", "query", "page", "country", "device", "searchAppearance"})
_SEARCH_TYPES = frozenset({"web", "image", "video", "news", "discover", "googleNews"})
_FILTER_OPERATORS = frozenset(
    {"equals", "notEquals", "contains", "notContains", "includingRegex", "excludingRegex"}
)
_MAX_FILTERS = 10
_INDEX_STATUS_FIELDS = (
    "verdict",
    "coverageState",
    "robotsTxtState",
    "indexingState",
    "pageFetchState",
    "lastCrawlTime",
    "googleCanonical",
    "userCanonical",
    "crawledAs",
)


def _site_url(value: str) -> str:
    site = value.strip()
    if not (site.startswith(("http://", "https://")) or site.startswith("sc-domain:")):
        raise GoogleMCPError("site_url must be a URL-prefix property or 'sc-domain:<domain>'")
    return site


def _filters(filters: list[dict[str, str]]) -> list[JSON]:
    if len(filters) > _MAX_FILTERS:
        raise GoogleMCPError(f"at most {_MAX_FILTERS} filters")
    out: list[JSON] = []
    for f in filters:
        dimension = f.get("dimension", "")
        operator = f.get("operator", "equals")
        expression = f.get("expression", "")
        if dimension not in _DIMENSIONS - {"date"}:
            raise GoogleMCPError(
                f"filter dimension must be one of {sorted(_DIMENSIONS - {'date'})}"
            )
        if operator not in _FILTER_OPERATORS:
            raise GoogleMCPError(f"filter operator must be one of {sorted(_FILTER_OPERATORS)}")
        if not expression:
            raise GoogleMCPError("filter expression is required")
        out.append({"dimension": dimension, "operator": operator, "expression": expression})
    return out


class SearchConsoleAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("searchconsole_list_sites", idempotent=True)
        def list_sites() -> JSON:
            """List Search Console properties the user can access, with permission level."""
            svc = ctx.client("searchconsole", "v1")
            resp = ctx.execute(svc.sites().list())
            return {
                "sites": [
                    pick(s, ("siteUrl", "permissionLevel")) for s in resp.get("siteEntry", [])
                ]
            }

        @registry.tool("searchconsole_query_search_analytics", idempotent=True)
        def query_search_analytics(
            site_url: str,
            start_date: str,
            end_date: str,
            dimensions: list[str] | None = None,
            search_type: str = "web",
            filters: list[dict[str, str]] | None = None,
            row_limit: int = 100,
            start_row: int = 0,
        ) -> JSON:
            """Query clicks, impressions, CTR, and position for a property.

            site_url: 'https://example.com/' or 'sc-domain:example.com'.
            Dates are YYYY-MM-DD (Pacific time), inclusive.
            dimensions: any of date, query, page, country, device, searchAppearance.
            filters: [{"dimension": "query", "operator": "contains", "expression": "mcp"}].
            Returns at most 1000 rows per call; page with start_row."""
            start, end = date_range(start_date, end_date)
            dims = field_names(
                dimensions or [],
                field="dimensions",
                max_count=len(_DIMENSIONS),
                required=False,
                allowed=_DIMENSIONS,
            )
            if search_type not in _SEARCH_TYPES:
                raise GoogleMCPError(f"search_type must be one of {sorted(_SEARCH_TYPES)}")
            if start_row < 0:
                raise GoogleMCPError("start_row must be 0 or greater")
            body: JSON = {
                "startDate": start,
                "endDate": end,
                "dimensions": dims,
                "type": search_type,
                "rowLimit": clamp_rows(row_limit),
                "startRow": start_row,
            }
            if filters:
                body["dimensionFilterGroups"] = [{"groupType": "and", "filters": _filters(filters)}]
            svc = ctx.client("searchconsole", "v1")
            resp = ctx.execute(svc.searchanalytics().query(siteUrl=_site_url(site_url), body=body))
            return {
                "dimensions": dims,
                "rows": [
                    pick(r, ("keys", "clicks", "impressions", "ctr", "position"))
                    for r in resp.get("rows", [])
                ],
                "aggregation": resp.get("responseAggregationType"),
            }

        @registry.tool("searchconsole_inspect_url", idempotent=True)
        def inspect_url(site_url: str, url: str, language_code: str = "en-US") -> JSON:
            """Inspect Google's index status for one URL inside a property.
            Limited by Google to 2,000 inspections per property per day."""
            site = _site_url(site_url)
            if not url.startswith(("http://", "https://")):
                raise GoogleMCPError("url must be an absolute http(s) URL")
            body = {"inspectionUrl": url, "siteUrl": site, "languageCode": language_code}
            svc = ctx.client("searchconsole", "v1")
            resp = ctx.execute(svc.urlInspection().index().inspect(body=body))
            result = resp.get("inspectionResult", {})
            return {
                "link": result.get("inspectionResultLink"),
                "index_status": pick(result.get("indexStatusResult", {}), _INDEX_STATUS_FIELDS),
                "rich_results": result.get("richResultsResult", {}).get("verdict"),
            }


ADAPTER = SearchConsoleAdapter()
