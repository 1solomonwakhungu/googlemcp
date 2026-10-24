from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.meet import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def test_create_space_with_access_type() -> None:
    ctx = FakeContext().respond(
        {
            "name": "spaces/abc",
            "meetingUri": "https://meet.google.com/abc-mnop-xyz",
            "meetingCode": "abc-mnop-xyz",
            "config": {"accessType": "TRUSTED"},
        }
    )
    out = call(registry_for(ADAPTER, ctx), "meet_create_space", access_type="TRUSTED")
    assert out["meetingUri"] == "https://meet.google.com/abc-mnop-xyz"
    req = ctx.requests[0]
    assert req.method == "POST"
    assert urlparse(req.uri).path == "/v2/spaces"
    assert req.body == {"config": {"accessType": "TRUSTED"}}


def test_get_space_accepts_meeting_code() -> None:
    ctx = FakeContext().respond({"name": "spaces/abc", "meetingCode": "abc-mnop-xyz", "extra": 1})
    out = call(registry_for(ADAPTER, ctx), "meet_get_space", space="abc-mnop-xyz")
    assert out == {"name": "spaces/abc", "meetingCode": "abc-mnop-xyz"}
    assert urlparse(ctx.requests[0].uri).path == "/v2/spaces/abc-mnop-xyz"


def test_list_conference_records() -> None:
    ctx = FakeContext().respond(
        {
            "conferenceRecords": [
                {"name": "conferenceRecords/c1", "startTime": "t0", "space": "spaces/abc", "x": 1}
            ],
            "nextPageToken": "n",
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "meet_list_conference_records",
        filter_expr='space.meeting_code = "abc-mnop-xyz"',
        page_size=500,
    )
    assert out == {
        "conference_records": [
            {"name": "conferenceRecords/c1", "startTime": "t0", "space": "spaces/abc"}
        ],
        "next_page_token": "n",
    }
    q = parse_qs(urlparse(ctx.requests[0].uri).query)
    assert q["pageSize"] == ["100"]
    assert q["filter"] == ['space.meeting_code = "abc-mnop-xyz"']


def test_list_participants_normalizes_parent() -> None:
    ctx = FakeContext().respond(
        {
            "participants": [
                {
                    "name": "conferenceRecords/c1/participants/p1",
                    "signedinUser": {"user": "users/1", "displayName": "Ada"},
                    "earliestStartTime": "t0",
                },
                {
                    "name": "conferenceRecords/c1/participants/p2",
                    "phoneUser": {"displayName": "+1***"},
                },
            ],
            "totalSize": 2,
        }
    )
    out = call(registry_for(ADAPTER, ctx), "meet_list_participants", conference_record="c1")
    assert urlparse(ctx.requests[0].uri).path == "/v2/conferenceRecords/c1/participants"
    assert out["participants"][0]["kind"] == "signedinUser"
    assert out["participants"][0]["display_name"] == "Ada"
    assert out["participants"][1]["kind"] == "phoneUser"
    assert out["total_size"] == 2


def test_list_transcripts_and_recordings_metadata() -> None:
    ctx = (
        FakeContext()
        .respond(
            {
                "transcripts": [
                    {
                        "name": "conferenceRecords/c1/transcripts/t1",
                        "state": "FILE_GENERATED",
                        "docsDestination": {"document": "doc1", "exportUri": "https://docs/x"},
                    }
                ]
            }
        )
        .respond(
            {
                "recordings": [
                    {
                        "name": "conferenceRecords/c1/recordings/r1",
                        "state": "STARTED",
                        "driveDestination": {"file": "f1", "exportUri": "https://drive/y"},
                    }
                ]
            }
        )
    )
    reg = registry_for(ADAPTER, ctx)
    transcripts = call(reg, "meet_list_transcripts", conference_record="conferenceRecords/c1")
    recordings = call(reg, "meet_list_recordings", conference_record="c1")

    assert transcripts["transcripts"][0] == {
        "name": "conferenceRecords/c1/transcripts/t1",
        "state": "FILE_GENERATED",
        "file": "doc1",
        "export_uri": "https://docs/x",
    }
    assert recordings["recordings"][0]["file"] == "f1"
    assert urlparse(ctx.requests[0].uri).path == "/v2/conferenceRecords/c1/transcripts"
    assert urlparse(ctx.requests[1].uri).path == "/v2/conferenceRecords/c1/recordings"


def test_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "meet_create_space", access_type="PUBLIC")
    with pytest.raises(GoogleMCPError):
        call(reg, "meet_get_space", space="conferenceRecords/c1")
    with pytest.raises(GoogleMCPError):
        call(reg, "meet_list_participants", conference_record=" ")
    assert ctx.requests == []


def test_write_flags() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    assert {t.name for t in reg.tools if t.write} == {"meet_create_space"}
    assert not any(t.destructive for t in reg.tools)


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 403, "status": "PERMISSION_DENIED"}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "meet_list_conference_records")
    assert info.value.status == 403
