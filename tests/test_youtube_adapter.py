from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.youtube import ADAPTER, SEARCH_CALL_BUDGET
from googlemcp.testing import FakeContext, call, registry_for


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def test_spec_scopes_are_least_privilege() -> None:
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/youtube.readonly",)
    assert ADAPTER.spec.write_scopes == ("https://www.googleapis.com/auth/youtube.force-ssl",)


def test_write_flags() -> None:
    tools = {t.name: t for t in registry_for(ADAPTER, FakeContext()).tools}
    writes = {n for n, t in tools.items() if t.write}
    assert writes == {
        "youtube_create_playlist",
        "youtube_add_playlist_item",
        "youtube_list_comment_threads",
    }
    assert not any(t.destructive for t in tools.values())


def test_list_my_channels_shapes_output() -> None:
    ctx = FakeContext().respond(
        {
            "items": [
                {
                    "id": "UC1",
                    "etag": "drop",
                    "snippet": {"title": "Me", "customUrl": "@me", "thumbnails": {}},
                    "statistics": {"subscriberCount": "5"},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "youtube_list_my_channels")
    assert out == {
        "channels": [
            {
                "id": "UC1",
                "title": "Me",
                "customUrl": "@me",
                "statistics": {"subscriberCount": "5"},
                "uploads_playlist_id": "UU1",
            }
        ]
    }
    q = _query(ctx.requests[0].uri)
    assert "/youtube/v3/channels" in ctx.requests[0].uri
    assert q["mine"] == ["true"]


def test_list_playlists_clamps_to_youtube_max() -> None:
    ctx = FakeContext().respond({"items": [], "nextPageToken": "n"})
    out = call(registry_for(ADAPTER, ctx), "youtube_list_my_playlists", max_results=500)
    assert out == {"playlists": [], "next_page_token": "n"}
    assert _query(ctx.requests[0].uri)["maxResults"] == ["50"]


def test_list_playlist_items_requires_id() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_list_playlist_items", playlist_id="  ")


def test_list_playlist_items_shapes_output() -> None:
    ctx = FakeContext().respond(
        {
            "items": [
                {
                    "id": "PI1",
                    "snippet": {
                        "title": "Clip",
                        "position": 0,
                        "resourceId": {"kind": "youtube#video", "videoId": "v1"},
                    },
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "youtube_list_playlist_items", playlist_id="PL1")
    assert out["items"] == [{"id": "PI1", "video_id": "v1", "title": "Clip", "position": 0}]
    assert _query(ctx.requests[0].uri)["playlistId"] == ["PL1"]


def test_get_videos_joins_ids_and_validates() -> None:
    ctx = FakeContext().respond({"items": [{"id": "a", "snippet": {"title": "A"}}]})
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "youtube_get_videos", video_ids=["a", " b ", ""])
    assert out["videos"][0]["id"] == "a"
    assert _query(ctx.requests[0].uri)["id"] == ["a,b"]
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_get_videos", video_ids=[])
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_get_videos", video_ids=[f"v{i}" for i in range(51)])


def test_search_is_capped_and_budgeted() -> None:
    ctx = FakeContext()
    for _ in range(SEARCH_CALL_BUDGET):
        ctx.respond(
            {
                "items": [
                    {"id": {"kind": "youtube#video", "videoId": "v"}, "snippet": {"title": "t"}}
                ]
            }
        )
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "youtube_search", query="mcp", max_results=100)
    assert out["results"] == [{"kind": "video", "id": "v", "title": "t"}]
    assert out["search_calls_remaining"] == SEARCH_CALL_BUDGET - 1
    q = _query(ctx.requests[0].uri)
    assert q["maxResults"] == ["25"]
    assert q["q"] == ["mcp"]
    assert q["type"] == ["video"]
    for _ in range(SEARCH_CALL_BUDGET - 1):
        call(reg, "youtube_search", query="mcp")
    with pytest.raises(GoogleMCPError, match="budget"):
        call(reg, "youtube_search", query="mcp")
    assert len(ctx.requests) == SEARCH_CALL_BUDGET


def test_search_validation_does_not_spend_budget() -> None:
    ctx = FakeContext().respond({"items": []})
    reg = registry_for(ADAPTER, ctx)
    for kwargs in (
        {"query": ""},
        {"query": "x", "kind": "movie"},
        {"query": "x", "order": "random"},
        {"query": "x", "kind": "channel", "for_mine": True},
    ):
        with pytest.raises(GoogleMCPError):
            call(reg, "youtube_search", **kwargs)
    out = call(reg, "youtube_search", query="x", for_mine=True)
    assert out["search_calls_remaining"] == SEARCH_CALL_BUDGET - 1
    assert _query(ctx.requests[0].uri)["forMine"] == ["true"]


def test_comment_threads_requires_exactly_one_target() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_list_comment_threads")
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_list_comment_threads", video_id="v", channel_id="c")


def test_comment_threads_by_channel() -> None:
    ctx = FakeContext().respond(
        {
            "items": [
                {
                    "id": "c1",
                    "snippet": {
                        "videoId": "v",
                        "totalReplyCount": 2,
                        "topLevelComment": {
                            "snippet": {"authorDisplayName": "a", "textOriginal": "hi"}
                        },
                    },
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "youtube_list_comment_threads", channel_id="UC1")
    assert out["threads"][0] == {
        "id": "c1",
        "video_id": "v",
        "author": "a",
        "text": "hi",
        "like_count": None,
        "published_at": None,
        "reply_count": 2,
    }
    q = _query(ctx.requests[0].uri)
    assert q["allThreadsRelatedToChannelId"] == ["UC1"]
    assert "videoId" not in q


def test_create_playlist_defaults_private() -> None:
    ctx = FakeContext().respond({"id": "PL9", "snippet": {"title": "New"}, "status": {}})
    out = call(registry_for(ADAPTER, ctx), "youtube_create_playlist", title="New")
    assert out["id"] == "PL9"
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.body == {
        "snippet": {"title": "New", "description": ""},
        "status": {"privacyStatus": "private"},
    }
    assert _query(req.uri)["part"] == ["snippet,status"]


def test_create_playlist_rejects_bad_privacy() -> None:
    with pytest.raises(GoogleMCPError):
        call(
            registry_for(ADAPTER, FakeContext()),
            "youtube_create_playlist",
            title="x",
            privacy_status="secret",
        )


def test_add_playlist_item_body() -> None:
    ctx = FakeContext().respond({"id": "PI2", "snippet": {"resourceId": {"videoId": "v"}}})
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "youtube_add_playlist_item", playlist_id="PL1", video_id="v", position=0)
    assert out == {"id": "PI2", "video_id": "v"}
    assert ctx.requests[0].body == {
        "snippet": {
            "playlistId": "PL1",
            "resourceId": {"kind": "youtube#video", "videoId": "v"},
            "position": 0,
        }
    }
    with pytest.raises(GoogleMCPError):
        call(reg, "youtube_add_playlist_item", playlist_id="PL1", video_id="v", position=-1)


def test_upstream_error_is_mapped() -> None:
    ctx = FakeContext().respond({"error": {"code": 403, "message": "quotaExceeded"}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "youtube_list_my_channels")
    assert info.value.status == 403
