from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.drive import ADAPTER, build_query, quote
from googlemcp.testing import FakeContext, call, registry_for


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def _tool(name: str):  # type: ignore[no-untyped-def]
    return next(t for t in registry_for(ADAPTER, FakeContext()).tools if t.name == name)


FILE = {
    "id": "f1",
    "name": "Budget",
    "mimeType": "application/vnd.google-apps.document",
    "owners": [{"displayName": "Me", "emailAddress": "me@example.com", "permissionId": "p"}],
    "kind": "drive#file",
    "capabilities": {"canEdit": True},
}


def test_quote_escapes_backslash_and_quote() -> None:
    assert quote("it's a\\b") == "'it\\'s a\\\\b'"


def test_build_query_wraps_raw_query_and_excludes_trash() -> None:
    q = build_query(
        query="name = 'a' or name = 'b'",
        name_contains="O'Brien",
        mime_type=None,
        folder_id="fold1",
        include_trashed=False,
    )
    assert q == (
        "(name = 'a' or name = 'b') and name contains 'O\\'Brien' "
        "and 'fold1' in parents and trashed = false"
    )
    assert (
        build_query(
            query=None, name_contains=None, mime_type=None, folder_id=None, include_trashed=True
        )
        is None
    )


def test_search_files_defaults_and_shaping() -> None:
    ctx = FakeContext().respond({"files": [FILE], "nextPageToken": "n2"})
    out = call(
        registry_for(ADAPTER, ctx), "drive_search_files", name_contains="Budget", max_results=999
    )
    req = ctx.requests[0]
    assert req.method == "GET"
    assert "/drive/v3/files?" in req.uri
    q = _query(req.uri)
    assert q["q"] == ["name contains 'Budget' and trashed = false"]
    assert q["pageSize"] == ["100"]
    assert q["corpora"] == ["user"]
    assert q["orderBy"] == ["modifiedTime desc"]
    assert q["fields"][0].startswith("nextPageToken,incompleteSearch,files(")
    assert out["next_page_token"] == "n2"
    assert out["files"] == [
        {
            "id": "f1",
            "name": "Budget",
            "mimeType": "application/vnd.google-apps.document",
            "owners": [{"displayName": "Me", "emailAddress": "me@example.com"}],
        }
    ]


def test_search_rejects_unknown_order_by() -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError, match="order_by"):
        call(registry_for(ADAPTER, ctx), "drive_search_files", order_by="name; drop")
    assert ctx.requests == []


def test_get_file_requests_limited_fields() -> None:
    ctx = FakeContext().respond(FILE)
    out = call(registry_for(ADAPTER, ctx), "drive_get_file", file_id="f1")
    assert "/drive/v3/files/f1?" in ctx.requests[0].uri
    assert "capabilities" not in out and "kind" not in out


def test_read_google_doc_exports_plain_text_and_truncates() -> None:
    ctx = FakeContext().respond(FILE).respond("Hello world")
    out = call(registry_for(ADAPTER, ctx), "drive_read_file_text", file_id="f1", max_chars=5)
    assert out == {
        "id": "f1",
        "name": "Budget",
        "mimeType": "application/vnd.google-apps.document",
        "exported_as": "text/plain",
        "text": "Hello",
        "total_chars": 11,
        "truncated": True,
    }
    export_req = ctx.requests[1]
    assert "/files/f1/export?" in export_req.uri
    assert _query(export_req.uri)["mimeType"] == ["text/plain"]


def test_read_sheet_exports_csv() -> None:
    ctx = FakeContext().respond({"id": "s1", "mimeType": "application/vnd.google-apps.spreadsheet"})
    ctx.respond("a,b\n1,2\n")
    out = call(registry_for(ADAPTER, ctx), "drive_read_file_text", file_id="s1")
    assert out["text"] == "a,b\n1,2\n"
    assert _query(ctx.requests[1].uri)["mimeType"] == ["text/csv"]


def test_read_text_file_downloads_media() -> None:
    ctx = FakeContext().respond(
        {"id": "t1", "name": "a.md", "mimeType": "text/markdown", "size": "5"}
    )
    ctx.respond("# hi\n")
    out = call(registry_for(ADAPTER, ctx), "drive_read_file_text", file_id="t1")
    assert out["text"] == "# hi\n"
    assert out["exported_as"] is None
    assert _query(ctx.requests[1].uri)["alt"] == ["media"]


def test_read_refuses_binary_and_oversized_files() -> None:
    ctx = FakeContext().respond({"id": "p1", "mimeType": "image/png", "size": "10"})
    with pytest.raises(GoogleMCPError, match="image/png"):
        call(registry_for(ADAPTER, ctx), "drive_read_file_text", file_id="p1")
    ctx = FakeContext().respond(
        {"id": "b1", "mimeType": "text/plain", "size": str(50 * 1024 * 1024)}
    )
    with pytest.raises(GoogleMCPError, match="refusing"):
        call(registry_for(ADAPTER, ctx), "drive_read_file_text", file_id="b1")
    assert len(ctx.requests) == 1


def test_create_folder() -> None:
    ctx = FakeContext().respond(
        {"id": "d1", "name": "Reports", "mimeType": "application/vnd.google-apps.folder"}
    )
    call(registry_for(ADAPTER, ctx), "drive_create_folder", name="Reports", parent_id="root")
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.body == {
        "name": "Reports",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["root"],
    }


def test_upload_text_file_is_multipart() -> None:
    ctx = FakeContext().respond({"id": "u1", "name": "notes.txt"})
    out = call(
        registry_for(ADAPTER, ctx),
        "drive_upload_text_file",
        name="notes.txt",
        content="hello drive",
        as_google_doc=True,
    )
    assert out == {"id": "u1", "name": "notes.txt"}
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "/upload/drive/v3/files?" in req.uri
    assert _query(req.uri)["uploadType"] == ["multipart"]
    assert '"mimeType": "application/vnd.google-apps.document"' in req.body
    assert "hello drive" in req.body


def test_upload_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError, match="text type"):
        call(
            reg,
            "drive_upload_text_file",
            name="x.bin",
            content="x",
            mime_type="application/octet-stream",
        )
    with pytest.raises(GoogleMCPError, match="exceeds"):
        call(reg, "drive_upload_text_file", name="big.txt", content="x" * (1024 * 1024 + 1))
    with pytest.raises(GoogleMCPError, match="empty"):
        call(reg, "drive_upload_text_file", name="  ", content="x")
    assert ctx.requests == []


def test_update_metadata_and_trash_use_patch() -> None:
    ctx = FakeContext().respond({"id": "f1", "name": "New"}).respond({"id": "f1", "trashed": True})
    reg = registry_for(ADAPTER, ctx)
    call(reg, "drive_update_file_metadata", file_id="f1", name="New", starred=True)
    out = call(reg, "drive_trash_file", file_id="f1")
    assert out == {"id": "f1", "trashed": True}
    rename, trash = ctx.requests
    assert rename.method == "PATCH" and trash.method == "PATCH"
    assert rename.body == {"name": "New", "starred": True}
    assert trash.body == {"trashed": True}
    with pytest.raises(GoogleMCPError, match="nothing"):
        call(reg, "drive_update_file_metadata", file_id="f1")


def test_tool_flags_and_scopes() -> None:
    for name in ("drive_search_files", "drive_get_file", "drive_read_file_text"):
        assert not _tool(name).write
    for name in ("drive_create_folder", "drive_upload_text_file", "drive_update_file_metadata"):
        assert _tool(name).write and not _tool(name).destructive
    assert _tool("drive_trash_file").destructive
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/drive.readonly",)
    assert ADAPTER.spec.write_scopes == ("https://www.googleapis.com/auth/drive.file",)


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "drive_trash_file", file_id="not-mine")
    assert info.value.status == 404


def test_parent_outside_drive_file_scope_gets_actionable_error() -> None:
    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError, match=r"drive\.file") as info:
        call(registry_for(ADAPTER, ctx), "drive_create_folder", name="Sub", parent_id="foreign")
    assert info.value.status == 404
    assert "foreign" in str(info.value)
    assert "opened with or shared to this app" in str(info.value)

    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError, match="drive_create_folder"):
        call(
            registry_for(ADAPTER, ctx),
            "drive_upload_text_file",
            name="a.txt",
            content="x",
            parent_id="foreign",
        )


def test_write_to_foreign_file_gets_actionable_error() -> None:
    for tool, kwargs in (
        ("drive_trash_file", {}),
        ("drive_update_file_metadata", {"name": "New"}),
    ):
        ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
        with pytest.raises(UpstreamError, match=r"drive\.file") as info:
            call(registry_for(ADAPTER, ctx), tool, file_id="theirs", **kwargs)
        assert "opened with or shared to this app" in str(info.value)


def test_root_create_404_is_not_rewritten() -> None:
    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "drive_create_folder", name="Top")
    assert "drive.file" not in str(info.value)


def test_non_404_write_errors_pass_through() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "drive_trash_file", file_id="f1")
    assert info.value.status == 403
    assert "drive.file" not in str(info.value)
