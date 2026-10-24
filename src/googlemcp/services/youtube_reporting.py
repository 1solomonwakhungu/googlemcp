"""YouTube Reporting API v1 adapter (off by default in the catalog).

Reporting jobs are server-side: once created, YouTube generates a daily bulk
report for that job until it is deleted, and reports are kept for a limited
time (developers.google.com/youtube/reporting/v1/reports). Creating a job is
therefore a write tool even though Google authorizes ``jobs.create`` with the
read-only ``yt-analytics.readonly`` scope. No extra write scope is requested.

Report files are fetched by ``downloadUrl`` with the same credentials; this
adapter returns the URL and metadata, not the CSV contents.
"""

from __future__ import annotations

import re

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
    key="youtube_reporting",
    title="YouTube Reporting",
    api_services=("youtubereporting.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/yt-analytics.readonly",),
)

_REPORT_TYPE_ID = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_JOB_FIELDS = ("id", "reportTypeId", "name", "createTime", "expireTime", "systemManaged")
_REPORT_FIELDS = ("id", "jobId", "startTime", "endTime", "createTime", "downloadUrl")


class YouTubeReportingAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("youtube_reporting_list_report_types", idempotent=True)
        def list_report_types(page_size: int = 50, page_token: str | None = None) -> JSON:
            """List bulk report types available for the user's channel."""
            svc = ctx.client("youtubereporting", "v1")
            resp = ctx.execute(
                svc.reportTypes().list(pageSize=clamp_page_size(page_size), pageToken=page_token)
            )
            return {
                "report_types": [pick(i, ("id", "name")) for i in resp.get("reportTypes", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_reporting_list_jobs", idempotent=True)
        def list_jobs(page_size: int = 50, page_token: str | None = None) -> JSON:
            """List the user's YouTube reporting jobs."""
            svc = ctx.client("youtubereporting", "v1")
            resp = ctx.execute(
                svc.jobs().list(pageSize=clamp_page_size(page_size), pageToken=page_token)
            )
            return {
                "jobs": [pick(i, _JOB_FIELDS) for i in resp.get("jobs", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_reporting_list_reports", idempotent=True)
        def list_reports(
            job_id: str,
            created_after: str | None = None,
            page_size: int = 50,
            page_token: str | None = None,
        ) -> JSON:
            """List generated reports for a job. ``created_after`` is an RFC 3339
            timestamp. Each report has a downloadUrl for the CSV file."""
            if not job_id.strip():
                raise GoogleMCPError("job_id is required")
            svc = ctx.client("youtubereporting", "v1")
            resp = ctx.execute(
                svc.jobs()
                .reports()
                .list(
                    jobId=job_id,
                    createdAfter=created_after,
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                )
            )
            return {
                "reports": [pick(i, _REPORT_FIELDS) for i in resp.get("reports", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_reporting_create_job", write=True)
        def create_job(report_type_id: str, job_name: str) -> JSON:
            """Create a server-side reporting job. YouTube then generates a daily
            report for this type until the job is deleted (in the YouTube
            Reporting API, outside this tool). Use list_report_types for IDs."""
            if not _REPORT_TYPE_ID.match(report_type_id):
                raise GoogleMCPError("report_type_id must look like 'channel_basic_a2'")
            if not job_name.strip():
                raise GoogleMCPError("job_name is required")
            svc = ctx.client("youtubereporting", "v1")
            body = {"reportTypeId": report_type_id, "name": job_name}
            return pick(ctx.execute(svc.jobs().create(body=body)), _JOB_FIELDS)


ADAPTER = YouTubeReportingAdapter()
