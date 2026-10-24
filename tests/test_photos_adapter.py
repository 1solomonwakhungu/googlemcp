"""Photos uses REST through ``authorized_session``; these tests fake that session."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests
from google.auth.exceptions import (
    DefaultCredentialsError,
    GoogleAuthError,
    MalformedError,
    RefreshError,
    TransportError,
)

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.photos import ADAPTER, LIBRARY, MAX_UPLOAD_BYTES, PICKER
from googlemcp.testing import FakeContext, call, registry_for


@dataclass
class FakeResponse:
    status_code: int
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)


@dataclass
class FakeSession:
    responses: list[FakeResponse] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


@dataclass
class RestContext(FakeContext):
    session: FakeSession = field(default_factory=FakeSession)

    def reply(self, payload: Any, status: int = 200) -> RestContext:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.session.responses.append(FakeResponse(status, body))
        return self

    def authorized_session(self) -> FakeSession:
        return self.session


def test_scopes_match_post_2025_photos_policy() -> None:
    removed = {"photoslibrary", "photoslibrary.readonly", "photoslibrary.sharing"}
    scopes = {s.rsplit("/", 1)[-1] for s in ADAPTER.spec.read_scopes + ADAPTER.spec.write_scopes}
    assert scopes == {
        "photoslibrary.readonly.appcreateddata",
        "photospicker.mediaitems.readonly",
        "photoslibrary.appendonly",
    }
    assert not scopes & removed


def test_write_flags() -> None:
    tools = registry_for(ADAPTER, RestContext()).tools
    assert {t.name for t in tools if t.write} == {"photos_create_album", "photos_upload_media_item"}
    assert not any(t.destructive for t in tools)


def test_list_app_albums() -> None:
    ctx = RestContext().reply(
        {"albums": [{"id": "a1", "title": "Trip", "isWriteable": True}], "nextPageToken": "p"}
    )
    out = call(registry_for(ADAPTER, ctx), "photos_list_app_albums", page_size=100)
    assert out == {"albums": [{"id": "a1", "title": "Trip"}], "next_page_token": "p"}
    c = ctx.session.calls[0]
    assert (c["method"], c["url"]) == ("GET", f"{LIBRARY}/albums")
    assert c["params"] == {"pageSize": 50}


def test_list_app_media_items_all_and_by_album() -> None:
    item = {
        "id": "m1",
        "filename": "a.jpg",
        "mimeType": "image/jpeg",
        "baseUrl": "https://lh3/x",
        "mediaMetadata": {"creationTime": "2026-01-01T00:00:00Z", "width": "10"},
    }
    ctx = RestContext().reply({"mediaItems": [item]}).reply({"mediaItems": []})
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "photos_list_app_media_items")
    assert out["media_items"] == [
        {
            "id": "m1",
            "filename": "a.jpg",
            "mimeType": "image/jpeg",
            "baseUrl": "https://lh3/x",
            "creationTime": "2026-01-01T00:00:00Z",
            "width": "10",
        }
    ]
    assert ctx.session.calls[0]["url"] == f"{LIBRARY}/mediaItems"
    call(reg, "photos_list_app_media_items", album_id="a1", page_token="t")
    c = ctx.session.calls[1]
    assert (c["method"], c["url"]) == ("POST", f"{LIBRARY}/mediaItems:search")
    assert c["json"] == {"albumId": "a1", "pageSize": 25, "pageToken": "t"}


def test_ids_cannot_inject_paths() -> None:
    ctx = RestContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "photos_get_picker_session", session_id="../albums")
    with pytest.raises(GoogleMCPError):
        call(reg, "photos_list_app_media_items", album_id="a/b")
    assert ctx.session.calls == []


def test_create_album() -> None:
    ctx = RestContext().reply({"id": "a2", "title": "New", "productUrl": "https://p"})
    out = call(registry_for(ADAPTER, ctx), "photos_create_album", title="New")
    assert out == {"id": "a2", "title": "New", "productUrl": "https://p"}
    assert ctx.session.calls[0]["json"] == {"album": {"title": "New"}}


def test_upload_two_step_flow() -> None:
    raw = b"\xff\xd8\xff fake jpeg"
    ctx = (
        RestContext()
        .reply("UPLOAD-TOKEN\n")
        .reply(
            {
                "newMediaItemResults": [
                    {
                        "uploadToken": "UPLOAD-TOKEN",
                        "status": {"message": "Success"},
                        "mediaItem": {"id": "m9", "filename": "a.jpg", "mimeType": "image/jpeg"},
                    }
                ]
            }
        )
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "photos_upload_media_item",
        filename="a.jpg",
        mime_type="image/jpeg",
        content_base64=base64.b64encode(raw).decode(),
        album_id="a1",
        description="hello",
    )
    assert out == {"id": "m9", "filename": "a.jpg", "mimeType": "image/jpeg"}
    upload, create = ctx.session.calls
    assert (upload["method"], upload["url"]) == ("POST", f"{LIBRARY}/uploads")
    assert upload["data"] == raw
    assert upload["headers"] == {
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": "image/jpeg",
        "X-Goog-Upload-Protocol": "raw",
    }
    assert create["url"] == f"{LIBRARY}/mediaItems:batchCreate"
    assert create["json"] == {
        "newMediaItems": [
            {
                "simpleMediaItem": {"fileName": "a.jpg", "uploadToken": "UPLOAD-TOKEN"},
                "description": "hello",
            }
        ],
        "albumId": "a1",
    }


def test_upload_item_level_failure() -> None:
    ctx = (
        RestContext()
        .reply("tok")
        .reply({"newMediaItemResults": [{"status": {"code": 3, "message": "bad"}}]})
    )
    with pytest.raises(UpstreamError, match="code 3"):
        call(
            registry_for(ADAPTER, ctx),
            "photos_upload_media_item",
            filename="a.jpg",
            mime_type="image/jpeg",
            content_base64=base64.b64encode(b"x").decode(),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"filename": "../etc/passwd"},
        {"filename": ""},
        {"mime_type": "application/pdf"},
        {"content_base64": "not base64!!"},
        {"content_base64": ""},
        {"content_base64": "A" * ((MAX_UPLOAD_BYTES * 4) // 3 + 8)},
        {"album_id": "x/y"},
        {"description": "d" * 1001},
    ],
)
def test_upload_validation_happens_before_network(kwargs: dict[str, str]) -> None:
    args = {
        "filename": "a.jpg",
        "mime_type": "image/jpeg",
        "content_base64": base64.b64encode(b"x").decode(),
        **kwargs,
    }
    ctx = RestContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "photos_upload_media_item", **args)
    assert ctx.session.calls == []


def test_picker_session_flow() -> None:
    session = {
        "id": "s1",
        "pickerUri": "https://photos.google.com/picker/s1",
        "pollingConfig": {"pollInterval": "5s", "timeoutIn": "1800s"},
        "expireTime": "2026-10-06T00:00:00Z",
    }
    ctx = (
        RestContext()
        .reply(session)
        .reply({**session, "mediaItemsSet": True})
        .reply(
            {
                "mediaItems": [
                    {
                        "id": "p1",
                        "createTime": "2026-01-01T00:00:00Z",
                        "type": "PHOTO",
                        "mediaFile": {
                            "baseUrl": "https://lh3/p1",
                            "mimeType": "image/jpeg",
                            "filename": "p1.jpg",
                            "mediaFileMetadata": {"width": 1},
                        },
                    }
                ],
                "nextPageToken": "n",
            }
        )
    )
    reg = registry_for(ADAPTER, ctx)
    created = call(reg, "photos_create_picker_session", max_item_count=5)
    assert created == {
        "id": "s1",
        "picker_uri": "https://photos.google.com/picker/s1",
        "media_items_set": False,
        "expire_time": "2026-10-06T00:00:00Z",
        "poll_interval": "5s",
        "timeout_in": "1800s",
    }
    assert ctx.session.calls[0]["url"] == f"{PICKER}/sessions"
    assert ctx.session.calls[0]["json"] == {"pickingConfig": {"maxItemCount": "5"}}

    assert call(reg, "photos_get_picker_session", session_id="s1")["media_items_set"] is True
    assert ctx.session.calls[1]["url"] == f"{PICKER}/sessions/s1"

    picked = call(reg, "photos_list_picked_media_items", session_id="s1", page_size=500)
    assert picked == {
        "media_items": [
            {
                "id": "p1",
                "createTime": "2026-01-01T00:00:00Z",
                "type": "PHOTO",
                "baseUrl": "https://lh3/p1",
                "mimeType": "image/jpeg",
                "filename": "p1.jpg",
            }
        ],
        "next_page_token": "n",
    }
    assert ctx.session.calls[2]["params"] == {"sessionId": "s1", "pageSize": 100}


def test_picker_max_items_validated() -> None:
    ctx = RestContext()
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "photos_create_picker_session", max_item_count=0)
    assert ctx.session.calls == []


def test_picked_items_before_user_finishes() -> None:
    ctx = RestContext().reply(
        {"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "secret detail"}},
        status=400,
    )
    with pytest.raises(GoogleMCPError, match="not finished picking") as info:
        call(registry_for(ADAPTER, ctx), "photos_list_picked_media_items", session_id="s1")
    assert not isinstance(info.value, UpstreamError)


def test_upstream_error_hides_body() -> None:
    ctx = RestContext().reply(
        {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "echo of request"}},
        status=403,
    )
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "photos_list_app_albums")
    assert info.value.status == 403
    assert info.value.reason == "PERMISSION_DENIED"
    assert "echo" not in str(info.value)


def test_non_json_error_body() -> None:
    ctx = RestContext().reply("<html>bad gateway</html>", status=502)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "photos_list_app_albums")
    assert info.value.reason is None
    assert str(info.value) == "Google API error 502"


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("dns failure for host secret.example"),
        requests.Timeout("read timed out with token ya29.secret"),
        ConnectionError("socket closed"),
        TransportError("transport failed for 1//0secret-refresh-token"),
    ],
)
def test_network_errors_are_mapped_and_sanitized(error: Exception) -> None:
    ctx = RestContext()
    ctx.session.error = error
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "photos_list_app_albums")
    assert info.value.status is None
    assert info.value.reason == "network"
    assert str(info.value) == f"network error calling Google Photos ({type(error).__name__})"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_refresh_error_is_mapped_without_echoing_it() -> None:
    # Regression: RefreshError text must never surface.
    ctx = RestContext()
    ctx.session.error = RefreshError("private refresh token echoed")
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "photos_list_app_albums")
    assert info.value.status == 401
    assert info.value.reason == "refresh_failed"
    assert "private refresh token echoed" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


@pytest.mark.parametrize(
    "error",
    [
        DefaultCredentialsError("private detail one"),
        GoogleAuthError("private detail two"),
        MalformedError("private detail three"),
    ],
)
def test_any_google_auth_error_is_mapped(error: Exception) -> None:
    ctx = RestContext()
    ctx.session.error = error
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "photos_list_app_albums")
    assert info.value.status == 401
    assert info.value.reason == "refresh_failed"
    assert "private detail" not in str(info.value)
    assert info.value.__cause__ is None


def test_refresh_error_while_creating_session_is_mapped() -> None:
    class RefreshingContext(RestContext):
        def authorized_session(self) -> FakeSession:
            raise RefreshError("private refresh token echoed")

    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, RefreshingContext()), "photos_list_app_albums")
    assert info.value.reason == "refresh_failed"
    assert "private" not in str(info.value)
