"""Google Meet REST API v2 adapter.

Off by default in the catalog. Google's Meet REST API quickstart lists "A Google
Workspace account with Google Meet enabled" as a prerequisite, so a consumer
@gmail.com account should not expect these tools to work.
Reference: https://developers.google.com/workspace/meet/api/guides/quickstart/python

The API supports user authentication only (no app-only access). Recordings and
transcripts are listed as metadata; their content lives in the organizer's
Drive and is not fetched here.
Reference: https://developers.google.com/workspace/meet/api/guides/authenticate-authorize

Scopes: ``meetings.space.readonly`` (sensitive) reads metadata for any space
the user can access. ``meetings.space.created`` (sensitive) is requested only
for writes and allows creating spaces.
"""

from __future__ import annotations

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
    key="meet",
    title="Google Meet",
    api_services=("meet.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/meetings.space.readonly",),
    write_scopes=("https://www.googleapis.com/auth/meetings.space.created",),
)

_ACCESS_TYPES = ("OPEN", "TRUSTED", "RESTRICTED")
_SPACE_FIELDS = ("name", "meetingUri", "meetingCode", "config", "activeConference")
_RECORD_FIELDS = ("name", "startTime", "endTime", "expireTime", "space")
_ARTIFACT_FIELDS = ("name", "state", "startTime", "endTime")


def _resource_name(collection: str, value: str) -> str:
    """Accept either ``<collection>/<id>`` or a bare id."""
    value = value.strip()
    if not value:
        raise GoogleMCPError(f"{collection} id must not be empty")
    if value.startswith(f"{collection}/"):
        return value
    if "/" in value:
        raise GoogleMCPError(f"expected '{collection}/<id>' or a bare id, got {value!r}")
    return f"{collection}/{value}"


def _participant(p: JSON) -> JSON:
    who = p.get("signedinUser") or p.get("anonymousUser") or p.get("phoneUser") or {}
    kind = next((k for k in ("signedinUser", "anonymousUser", "phoneUser") if k in p), "unknown")
    return {
        "name": p.get("name"),
        "kind": kind,
        "display_name": who.get("displayName"),
        "user": who.get("user"),
        "earliest_start_time": p.get("earliestStartTime"),
        "latest_end_time": p.get("latestEndTime"),
    }


def _artifact(a: JSON, destination_key: str) -> JSON:
    out = pick(a, _ARTIFACT_FIELDS)
    dest = a.get(destination_key, {})
    out["file"] = dest.get("file") or dest.get("document")
    out["export_uri"] = dest.get("exportUri")
    return out


class MeetAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("meet_create_space", write=True)
        def create_space(access_type: str | None = None) -> JSON:
            """Create a Meet meeting space and return its join URL.

            ``access_type`` is OPEN, TRUSTED, or RESTRICTED; omit it to use the
            account default.
            """
            body: JSON = {}
            if access_type is not None:
                if access_type not in _ACCESS_TYPES:
                    raise GoogleMCPError(f"access_type must be one of {', '.join(_ACCESS_TYPES)}")
                body["config"] = {"accessType": access_type}
            svc = ctx.client("meet", "v2")
            return pick(ctx.execute(svc.spaces().create(body=body)), _SPACE_FIELDS)

        @registry.tool("meet_get_space")
        def get_space(space: str) -> JSON:
            """Get a meeting space by 'spaces/<id>', a bare id, or a meeting code (abc-mnop-xyz)."""
            svc = ctx.client("meet", "v2")
            return pick(
                ctx.execute(svc.spaces().get(name=_resource_name("spaces", space))), _SPACE_FIELDS
            )

        @registry.tool("meet_list_conference_records")
        def list_conference_records(
            filter_expr: str | None = None,
            page_size: int = 25,
            page_token: str | None = None,
        ) -> JSON:
            """List past and active conferences, newest first.

            ``filter_expr`` uses the API filter syntax, for example
            'space.meeting_code = "abc-mnop-xyz"' or 'start_time >= "2026-10-01T00:00:00Z"'.
            """
            svc = ctx.client("meet", "v2")
            resp = ctx.execute(
                svc.conferenceRecords().list(
                    filter=filter_expr, pageSize=clamp_page_size(page_size), pageToken=page_token
                )
            )
            return {
                "conference_records": [
                    pick(r, _RECORD_FIELDS) for r in resp.get("conferenceRecords", [])
                ],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("meet_list_participants")
        def list_participants(
            conference_record: str, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List participants of a conference record."""
            svc = ctx.client("meet", "v2")
            resp = ctx.execute(
                svc.conferenceRecords()
                .participants()
                .list(
                    parent=_resource_name("conferenceRecords", conference_record),
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                )
            )
            return {
                "participants": [_participant(p) for p in resp.get("participants", [])],
                "total_size": resp.get("totalSize"),
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("meet_list_transcripts")
        def list_transcripts(
            conference_record: str, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List transcript metadata (state, times, Google Docs location) for a conference."""
            svc = ctx.client("meet", "v2")
            resp = ctx.execute(
                svc.conferenceRecords()
                .transcripts()
                .list(
                    parent=_resource_name("conferenceRecords", conference_record),
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                )
            )
            return {
                "transcripts": [
                    _artifact(t, "docsDestination") for t in resp.get("transcripts", [])
                ],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("meet_list_recordings")
        def list_recordings(
            conference_record: str, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List recording metadata (state, times, Drive file location) for a conference."""
            svc = ctx.client("meet", "v2")
            resp = ctx.execute(
                svc.conferenceRecords()
                .recordings()
                .list(
                    parent=_resource_name("conferenceRecords", conference_record),
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                )
            )
            return {
                "recordings": [
                    _artifact(r, "driveDestination") for r in resp.get("recordings", [])
                ],
                "next_page_token": resp.get("nextPageToken"),
            }


ADAPTER = MeetAdapter()
