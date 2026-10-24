import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.driveactivity import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for

ACTIVITY = {
    "primaryActionDetail": {"rename": {"oldTitle": "Draft", "newTitle": "Final"}},
    "actors": [
        {"user": {"knownUser": {"personName": "people/123", "isCurrentUser": True}}},
        {"system": {"type": "TRASH_AUTO_PURGE"}},
        {"user": {"deletedUser": {}}},
    ],
    "actions": [{"detail": {"rename": {}}}],
    "targets": [
        {
            "driveItem": {
                "name": "items/f1",
                "title": "Final",
                "mimeType": "application/vnd.google-apps.document",
                "driveFile": {},
                "owner": {"user": {}},
            }
        },
        {"drive": {"name": "drives/d1", "title": "Team", "root": {}}},
        {
            "fileComment": {
                "linkToDiscussion": "https://docs.google.com/x",
                "legacyCommentId": "c1",
                "parent": {"name": "items/f2", "title": "Doc", "driveFolder": {}},
            }
        },
    ],
    "timestamp": "2026-10-05T12:00:00Z",
}


def test_query_for_item_builds_body_and_shapes() -> None:
    ctx = FakeContext().respond({"activities": [ACTIVITY], "nextPageToken": "n"})
    out = call(
        registry_for(ADAPTER, ctx),
        "driveactivity_query_activity",
        item_id="f1",
        filter="detail.action_detail_case:RENAME",
        max_results=500,
    )
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.uri.split("?")[0].endswith("/v2/activity:query")
    assert req.body == {
        "pageSize": 100,
        "itemName": "items/f1",
        "filter": "detail.action_detail_case:RENAME",
        "consolidationStrategy": {"none": {}},
    }
    assert out["next_page_token"] == "n"
    assert out["activities"] == [
        {
            "action": "rename",
            "detail": {"oldTitle": "Draft", "newTitle": "Final"},
            "timestamp": "2026-10-05T12:00:00Z",
            "actors": [
                {"type": "user", "person": "people/123", "is_current_user": True},
                {"type": "system"},
                {"type": "deletedUser"},
            ],
            "targets": [
                {
                    "type": "drive_item",
                    "name": "items/f1",
                    "title": "Final",
                    "mimeType": "application/vnd.google-apps.document",
                    "kind": "file",
                },
                {"type": "shared_drive", "name": "drives/d1", "title": "Team"},
                {
                    "type": "comment",
                    "linkToDiscussion": "https://docs.google.com/x",
                    "parent": {"name": "items/f2", "title": "Doc", "kind": "folder"},
                },
            ],
        }
    ]


def test_query_folder_with_consolidation_and_time_range() -> None:
    ctx = FakeContext().respond(
        {
            "activities": [
                {
                    "primaryActionDetail": {"edit": {}},
                    "timeRange": {"startTime": "a", "endTime": "b"},
                }
            ]
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "driveactivity_query_activity",
        folder_id="items/fold",
        consolidate=True,
        page_token="p2",
    )
    assert ctx.requests[0].body == {
        "pageSize": 25,
        "ancestorName": "items/fold",
        "pageToken": "p2",
        "consolidationStrategy": {"legacy": {}},
    }
    assert out["activities"][0] == {
        "action": "edit",
        "time_range": {"startTime": "a", "endTime": "b"},
        "actors": [],
        "targets": [],
    }


def test_large_detail_is_dropped() -> None:
    big = {"primaryActionDetail": {"permissionChange": {"addedPermissions": [{"x": "y" * 3000}]}}}
    ctx = FakeContext().respond({"activities": [big]})
    out = call(registry_for(ADAPTER, ctx), "driveactivity_query_activity")
    assert out["activities"][0]["detail"] == {"truncated": True}
    assert "itemName" not in ctx.requests[0].body


def test_item_and_folder_are_exclusive() -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "driveactivity_query_activity", item_id="a", folder_id="b")
    assert ctx.requests == []


def test_read_only_spec() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    assert all(not t.write for t in reg.tools)
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/drive.activity.readonly",)
    assert ADAPTER.spec.write_scopes == ()


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 400}}, status=400)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "driveactivity_query_activity", filter="bogus")
    assert info.value.status == 400
