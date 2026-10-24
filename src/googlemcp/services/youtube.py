"""YouTube Data API v3 adapter.

Quota: every project gets 10,000 units per day by default. Unit costs per call
are noted in each tool docstring (source: developers.google.com/youtube/v3/
determine_quota_cost). ``search.list`` costs 100 units, so the search tool is
capped per call and per server process. Uploads are out of scope.

Scopes: ``youtube.readonly`` covers every read tool except comment threads.
Google accepts only ``youtube.force-ssl`` for ``commentThreads.list``, and that
scope also grants account management, so the comment tool is registered as a
write tool and is only exposed when writes are enabled for this product.
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
    key="youtube",
    title="YouTube Data",
    api_services=("youtube.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/youtube.readonly",),
    write_scopes=("https://www.googleapis.com/auth/youtube.force-ssl",),
)

# YouTube list endpoints accept at most 50 results per page.
YOUTUBE_MAX_RESULTS = 50
# search.list costs 100 units; keep a single call small.
SEARCH_MAX_RESULTS = 25
# Per-process ceiling on search.list calls (20 x 100 = 2,000 units, 20% of the
# default daily quota). Restarting the server resets the budget.
SEARCH_CALL_BUDGET = 20
_MAX_VIDEO_IDS = 50

_SEARCH_TYPES = frozenset({"video", "channel", "playlist"})
_SEARCH_ORDERS = frozenset({"relevance", "date", "rating", "title", "viewCount", "videoCount"})
_PRIVACY = frozenset({"private", "unlisted", "public"})
_COMMENT_ORDERS = frozenset({"time", "relevance"})


def _max_results(value: int | None) -> int:
    return min(clamp_page_size(value), YOUTUBE_MAX_RESULTS)


def _channel(item: JSON) -> JSON:
    snippet = item.get("snippet", {})
    return {
        "id": item.get("id"),
        **pick(snippet, ("title", "description", "customUrl", "publishedAt")),
        "statistics": item.get("statistics", {}),
        "uploads_playlist_id": item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads"),
    }


def _playlist(item: JSON) -> JSON:
    snippet = item.get("snippet", {})
    return {
        "id": item.get("id"),
        **pick(snippet, ("title", "description", "publishedAt", "channelId")),
        "privacy_status": item.get("status", {}).get("privacyStatus"),
        "item_count": item.get("contentDetails", {}).get("itemCount"),
    }


def _playlist_item(item: JSON) -> JSON:
    snippet = item.get("snippet", {})
    return {
        "id": item.get("id"),
        "video_id": snippet.get("resourceId", {}).get("videoId"),
        **pick(snippet, ("title", "position", "publishedAt", "videoOwnerChannelTitle")),
    }


def _video(item: JSON) -> JSON:
    snippet = item.get("snippet", {})
    return {
        "id": item.get("id"),
        **pick(snippet, ("title", "description", "channelId", "channelTitle", "publishedAt")),
        "duration": item.get("contentDetails", {}).get("duration"),
        "privacy_status": item.get("status", {}).get("privacyStatus"),
        "statistics": item.get("statistics", {}),
    }


def _search_result(item: JSON) -> JSON:
    ident = item.get("id", {})
    snippet = item.get("snippet", {})
    return {
        "kind": ident.get("kind", "").removeprefix("youtube#"),
        "id": ident.get("videoId") or ident.get("channelId") or ident.get("playlistId"),
        **pick(snippet, ("title", "description", "channelId", "channelTitle", "publishedAt")),
    }


def _comment_thread(item: JSON) -> JSON:
    snippet = item.get("snippet", {})
    top = snippet.get("topLevelComment", {}).get("snippet", {})
    return {
        "id": item.get("id"),
        "video_id": snippet.get("videoId"),
        "author": top.get("authorDisplayName"),
        "text": top.get("textOriginal", top.get("textDisplay")),
        "like_count": top.get("likeCount"),
        "published_at": top.get("publishedAt"),
        "reply_count": snippet.get("totalReplyCount"),
    }


class YouTubeAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        search_calls = 0

        @registry.tool("youtube_list_my_channels", idempotent=True)
        def list_my_channels() -> JSON:
            """List the YouTube channels owned by the signed-in user. Costs 1 quota unit."""
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(
                svc.channels().list(part="snippet,statistics,contentDetails", mine=True)
            )
            return {"channels": [_channel(i) for i in resp.get("items", [])]}

        @registry.tool("youtube_list_my_playlists", idempotent=True)
        def list_my_playlists(max_results: int = 25, page_token: str | None = None) -> JSON:
            """List playlists owned by the signed-in user. Costs 1 quota unit per page."""
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(
                svc.playlists().list(
                    part="snippet,status,contentDetails",
                    mine=True,
                    maxResults=_max_results(max_results),
                    pageToken=page_token,
                )
            )
            return {
                "playlists": [_playlist(i) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_list_playlist_items", idempotent=True)
        def list_playlist_items(
            playlist_id: str, max_results: int = 25, page_token: str | None = None
        ) -> JSON:
            """List videos in a playlist. Costs 1 quota unit per page."""
            if not playlist_id.strip():
                raise GoogleMCPError("playlist_id is required")
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(
                svc.playlistItems().list(
                    part="snippet",
                    playlistId=playlist_id,
                    maxResults=_max_results(max_results),
                    pageToken=page_token,
                )
            )
            return {
                "items": [_playlist_item(i) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_get_videos", idempotent=True)
        def get_videos(video_ids: list[str]) -> JSON:
            """Get details and statistics for up to 50 videos by ID. Costs 1 quota unit."""
            ids = [v.strip() for v in video_ids if v and v.strip()]
            if not ids:
                raise GoogleMCPError("video_ids must contain at least one ID")
            if len(ids) > _MAX_VIDEO_IDS:
                raise GoogleMCPError(f"at most {_MAX_VIDEO_IDS} video IDs per call")
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(
                svc.videos().list(part="snippet,contentDetails,statistics,status", id=",".join(ids))
            )
            return {"videos": [_video(i) for i in resp.get("items", [])]}

        @registry.tool("youtube_search")
        def search(
            query: str,
            kind: str = "video",
            order: str = "relevance",
            max_results: int = 10,
            for_mine: bool = False,
            page_token: str | None = None,
        ) -> JSON:
            """Search YouTube. EXPENSIVE: each call costs 100 quota units (1% of the
            default daily quota). At most 25 results per call and 20 calls per
            server process. Prefer the list tools when possible. ``for_mine``
            restricts results to the user's own videos (kind must be 'video')."""
            nonlocal search_calls
            if not query.strip():
                raise GoogleMCPError("query is required")
            if kind not in _SEARCH_TYPES:
                raise GoogleMCPError(f"kind must be one of {sorted(_SEARCH_TYPES)}")
            if order not in _SEARCH_ORDERS:
                raise GoogleMCPError(f"order must be one of {sorted(_SEARCH_ORDERS)}")
            if for_mine and kind != "video":
                raise GoogleMCPError("for_mine requires kind 'video'")
            if search_calls >= SEARCH_CALL_BUDGET:
                raise GoogleMCPError(
                    f"search budget of {SEARCH_CALL_BUDGET} calls for this server process "
                    "is exhausted; use list tools or restart the server"
                )
            search_calls += 1
            params: JSON = {
                "part": "snippet",
                "q": query,
                "type": kind,
                "order": order,
                "maxResults": min(clamp_page_size(max_results), SEARCH_MAX_RESULTS),
                "pageToken": page_token,
            }
            if for_mine:
                params["forMine"] = True
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(svc.search().list(**params))
            return {
                "results": [_search_result(i) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
                "search_calls_remaining": SEARCH_CALL_BUDGET - search_calls,
            }

        @registry.tool("youtube_list_comment_threads", write=True, idempotent=True)
        def list_comment_threads(
            video_id: str | None = None,
            channel_id: str | None = None,
            order: str = "time",
            max_results: int = 20,
            page_token: str | None = None,
        ) -> JSON:
            """Read top-level comment threads for a video, or for all videos of a
            channel. Read-only, but Google requires the youtube.force-ssl scope,
            so this tool is only available when YouTube writes are enabled.
            Give exactly one of video_id or channel_id. Costs 1 quota unit."""
            if bool(video_id) == bool(channel_id):
                raise GoogleMCPError("give exactly one of video_id or channel_id")
            if order not in _COMMENT_ORDERS:
                raise GoogleMCPError(f"order must be one of {sorted(_COMMENT_ORDERS)}")
            params: JSON = {
                "part": "snippet",
                "order": order,
                "textFormat": "plainText",
                "maxResults": _max_results(max_results),
                "pageToken": page_token,
            }
            if video_id:
                params["videoId"] = video_id
            else:
                params["allThreadsRelatedToChannelId"] = channel_id
            svc = ctx.client("youtube", "v3")
            resp = ctx.execute(svc.commentThreads().list(**params))
            return {
                "threads": [_comment_thread(i) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("youtube_create_playlist", write=True)
        def create_playlist(
            title: str, description: str = "", privacy_status: str = "private"
        ) -> JSON:
            """Create a playlist on the user's channel. Defaults to private.
            Costs 50 quota units."""
            if not title.strip():
                raise GoogleMCPError("title is required")
            if privacy_status not in _PRIVACY:
                raise GoogleMCPError(f"privacy_status must be one of {sorted(_PRIVACY)}")
            body: JSON = {
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": privacy_status},
            }
            svc = ctx.client("youtube", "v3")
            return _playlist(ctx.execute(svc.playlists().insert(part="snippet,status", body=body)))

        @registry.tool("youtube_add_playlist_item", write=True)
        def add_playlist_item(playlist_id: str, video_id: str, position: int | None = None) -> JSON:
            """Add a video to one of the user's playlists. ``position`` is 0-based;
            omit it to append. Costs 50 quota units."""
            if not playlist_id.strip() or not video_id.strip():
                raise GoogleMCPError("playlist_id and video_id are required")
            snippet: JSON = {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
            if position is not None:
                if position < 0:
                    raise GoogleMCPError("position must be 0 or greater")
                snippet["position"] = position
            svc = ctx.client("youtube", "v3")
            return _playlist_item(
                ctx.execute(svc.playlistItems().insert(part="snippet", body={"snippet": snippet}))
            )


ADAPTER = YouTubeAdapter()
