"""Google Photos adapter: Library API (app-created data only) and Picker API.

What changed on 2025-03-31 (developers.google.com/photos/support/updates):
the ``photoslibrary``, ``photoslibrary.readonly``, and ``photoslibrary.sharing``
scopes were removed. The Library API can now only list, search, and read
albums and media items that this app created, and shared-album methods
(``albums.share``, ``albums.unshare``, ``sharedAlbums.*``) return 403.

What is therefore NOT possible through this adapter:
- browsing or searching the user's whole photo library;
- reading or managing shared albums;
- deleting media (the Library API never supported deletion).

To work with photos the user already has, use the Picker API: create a
session, have the user open ``picker_uri`` and choose items, poll the session
until ``media_items_set`` is true, then list the picked items.

Neither API has a static discovery document in google-api-python-client, so
calls go through ``ctx.authorized_session()`` over REST.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from google.auth.exceptions import GoogleAuthError, TransportError

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    UpstreamError,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="photos",
    title="Google Photos (Library app-created data and Picker)",
    api_services=("photoslibrary.googleapis.com", "photospicker.googleapis.com"),
    read_scopes=(
        "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
        "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
    ),
    write_scopes=("https://www.googleapis.com/auth/photoslibrary.appendonly",),
)

LIBRARY = "https://photoslibrary.googleapis.com/v1"
PICKER = "https://photospicker.googleapis.com/v1"

# Upload bytes arrive base64-encoded inside a tool call, so keep them modest.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_ALBUM_PAGE_MAX = 50
_TIMEOUT = 30.0
_UPLOAD_TIMEOUT = 120.0
_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_MIME = re.compile(r"^(image|video)/[A-Za-z0-9.+-]{1,64}$")
_FILENAME = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")

_ALBUM_FIELDS = ("id", "title", "productUrl", "mediaItemsCount", "coverPhotoMediaItemId")
_MEDIA_FIELDS = ("id", "filename", "mimeType", "description", "productUrl", "baseUrl")


def _check_id(value: str, field: str) -> str:
    if not _ID.match(value or ""):
        raise GoogleMCPError(f"{field} is not a valid ID")
    return value


def _request(
    ctx: ServiceContext,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: JSON | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = _TIMEOUT,
    expect_json: bool = True,
) -> Any:
    """Send one REST call and map failures to ``UpstreamError``.

    Error messages include only the HTTP status and Google's status enum,
    never response bodies or exception text, which may echo request data or
    credentials. Exceptions are not chained for the same reason.
    """
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        session = ctx.authorized_session()
        resp = session.request(
            method,
            url,
            params=clean_params or None,
            json=json,
            data=data,
            headers=headers,
            timeout=timeout,
        )
    except TransportError as exc:
        # Network failure while refreshing the access token.
        raise UpstreamError(
            f"network error calling Google Photos ({type(exc).__name__})",
            status=None,
            reason="network",
        ) from None
    except GoogleAuthError:
        # RefreshError and any other google-auth failure from the session.
        raise UpstreamError(
            "Google token refresh failed; run `googlemcp auth login`",
            status=401,
            reason="refresh_failed",
        ) from None
    except OSError as exc:
        # requests.RequestException subclasses OSError.
        raise UpstreamError(
            f"network error calling Google Photos ({type(exc).__name__})",
            status=None,
            reason="network",
        ) from None
    status = int(resp.status_code)
    if status >= 400:
        reason = _error_status(resp)
        suffix = f" ({reason})" if reason else ""
        raise UpstreamError(f"Google API error {status}{suffix}", status=status, reason=reason)
    if not expect_json:
        return resp.text
    if not resp.content:
        return {}
    try:
        payload: JSON = resp.json()
    except ValueError as exc:
        raise UpstreamError("Google Photos returned a non-JSON response", status=status) from exc
    return payload


def _error_status(resp: Any) -> str | None:
    try:
        err = resp.json().get("error", {})
    except (ValueError, AttributeError):
        return None
    reason = err.get("status") if isinstance(err, dict) else None
    return reason if isinstance(reason, str) else None


def _media_item(item: JSON) -> JSON:
    meta = item.get("mediaMetadata", {})
    return {**pick(item, _MEDIA_FIELDS), **pick(meta, ("creationTime", "width", "height"))}


def _session(item: JSON) -> JSON:
    polling = item.get("pollingConfig", {})
    return {
        "id": item.get("id"),
        "picker_uri": item.get("pickerUri"),
        "media_items_set": bool(item.get("mediaItemsSet", False)),
        "expire_time": item.get("expireTime"),
        "poll_interval": polling.get("pollInterval"),
        "timeout_in": polling.get("timeoutIn"),
    }


def _picked_item(item: JSON) -> JSON:
    media = item.get("mediaFile", {})
    return {
        **pick(item, ("id", "createTime", "type")),
        **pick(media, ("baseUrl", "mimeType", "filename")),
    }


class PhotosAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("photos_list_app_albums", idempotent=True)
        def list_app_albums(page_size: int = 25, page_token: str | None = None) -> JSON:
            """List albums created by this app. Albums made in the Photos app or by
            other apps are not visible (Google API restriction since 2025-03-31)."""
            resp = _request(
                ctx,
                "GET",
                f"{LIBRARY}/albums",
                params={
                    "pageSize": min(clamp_page_size(page_size), _ALBUM_PAGE_MAX),
                    "pageToken": page_token,
                },
            )
            return {
                "albums": [pick(a, _ALBUM_FIELDS) for a in resp.get("albums", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("photos_list_app_media_items", idempotent=True)
        def list_app_media_items(
            album_id: str | None = None, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List media items uploaded by this app, optionally only those in one
            app-created album. baseUrl values expire after about 60 minutes."""
            size = clamp_page_size(page_size)
            if album_id:
                resp = _request(
                    ctx,
                    "POST",
                    f"{LIBRARY}/mediaItems:search",
                    json={
                        "albumId": _check_id(album_id, "album_id"),
                        "pageSize": size,
                        **({"pageToken": page_token} if page_token else {}),
                    },
                )
            else:
                resp = _request(
                    ctx,
                    "GET",
                    f"{LIBRARY}/mediaItems",
                    params={"pageSize": size, "pageToken": page_token},
                )
            return {
                "media_items": [_media_item(m) for m in resp.get("mediaItems", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("photos_create_album", write=True)
        def create_album(title: str) -> JSON:
            """Create an album owned by this app. The app can later add uploads to it."""
            if not title.strip() or len(title) > 500:
                raise GoogleMCPError("title must be 1 to 500 characters")
            resp = _request(ctx, "POST", f"{LIBRARY}/albums", json={"album": {"title": title}})
            return pick(resp, _ALBUM_FIELDS)

        @registry.tool("photos_upload_media_item", write=True)
        def upload_media_item(
            filename: str,
            mime_type: str,
            content_base64: str,
            album_id: str | None = None,
            description: str | None = None,
        ) -> JSON:
            """Upload one photo or video (base64, at most 20 MB decoded) to the
            user's library, optionally into an app-created album. Uses the
            append-only scope: the app cannot delete it afterwards."""
            if not _FILENAME.match(filename or ""):
                raise GoogleMCPError("filename must be 1 to 255 characters without slashes")
            if not _MIME.match(mime_type or ""):
                raise GoogleMCPError("mime_type must be an image/* or video/* type")
            if len(content_base64) > (MAX_UPLOAD_BYTES * 4) // 3 + 4:
                raise GoogleMCPError(f"content exceeds {MAX_UPLOAD_BYTES} bytes")
            try:
                raw = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise GoogleMCPError("content_base64 is not valid base64") from exc
            if not raw:
                raise GoogleMCPError("content is empty")
            if len(raw) > MAX_UPLOAD_BYTES:
                raise GoogleMCPError(f"content exceeds {MAX_UPLOAD_BYTES} bytes")
            if description is not None and len(description) > 1000:
                raise GoogleMCPError("description is limited to 1000 characters")
            album = _check_id(album_id, "album_id") if album_id else None

            token = _request(
                ctx,
                "POST",
                f"{LIBRARY}/uploads",
                data=raw,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Goog-Upload-Content-Type": mime_type,
                    "X-Goog-Upload-Protocol": "raw",
                },
                timeout=_UPLOAD_TIMEOUT,
                expect_json=False,
            ).strip()
            if not token:
                raise UpstreamError("Google Photos returned an empty upload token")

            new_item: JSON = {"simpleMediaItem": {"fileName": filename, "uploadToken": token}}
            if description:
                new_item["description"] = description
            body: JSON = {"newMediaItems": [new_item]}
            if album:
                body["albumId"] = album
            resp = _request(ctx, "POST", f"{LIBRARY}/mediaItems:batchCreate", json=body)
            results = resp.get("newMediaItemResults", [])
            if not results:
                raise UpstreamError("Google Photos returned no media item result")
            result = results[0]
            status = result.get("status", {})
            if status.get("code", 0) not in (0, None):
                raise UpstreamError(
                    f"Google Photos rejected the media item (code {status.get('code')})"
                )
            return _media_item(result.get("mediaItem", {}))

        @registry.tool("photos_create_picker_session")
        def create_picker_session(max_item_count: int | None = None) -> JSON:
            """Start a Google Photos Picker session so the user can choose existing
            photos. Give picker_uri to the user to open; then poll
            photos_get_picker_session until media_items_set is true."""
            body: JSON = {}
            if max_item_count is not None:
                if not 1 <= max_item_count <= 2000:
                    raise GoogleMCPError("max_item_count must be between 1 and 2000")
                body["pickingConfig"] = {"maxItemCount": str(max_item_count)}
            return _session(_request(ctx, "POST", f"{PICKER}/sessions", json=body))

        @registry.tool("photos_get_picker_session", idempotent=True)
        def get_picker_session(session_id: str) -> JSON:
            """Check a Picker session. media_items_set becomes true after the user
            finishes picking. Respect poll_interval between checks."""
            sid = _check_id(session_id, "session_id")
            return _session(_request(ctx, "GET", f"{PICKER}/sessions/{sid}"))

        @registry.tool("photos_list_picked_media_items", idempotent=True)
        def list_picked_media_items(
            session_id: str, page_size: int = 50, page_token: str | None = None
        ) -> JSON:
            """List the items the user picked in a Picker session. baseUrl needs
            the same OAuth token to download and expires after about 60 minutes."""
            sid = _check_id(session_id, "session_id")
            try:
                resp = _request(
                    ctx,
                    "GET",
                    f"{PICKER}/mediaItems",
                    params={
                        "sessionId": sid,
                        "pageSize": clamp_page_size(page_size),
                        "pageToken": page_token,
                    },
                )
            except UpstreamError as exc:
                if exc.reason == "FAILED_PRECONDITION":
                    raise GoogleMCPError(
                        "the user has not finished picking; poll photos_get_picker_session "
                        "until media_items_set is true"
                    ) from exc
                raise
            return {
                "media_items": [_picked_item(m) for m in resp.get("mediaItems", [])],
                "next_page_token": resp.get("nextPageToken"),
            }


ADAPTER = PhotosAdapter()
