import base64
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.gmail import ADAPTER, MAX_BODY_FETCH_BYTES, MAX_THREAD_CHARS, html_to_text
from googlemcp.testing import FakeContext, call, registry_for


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def _tool(name: str):  # type: ignore[no-untyped-def]
    return next(t for t in registry_for(ADAPTER, FakeContext()).tools if t.name == name)


FULL_MESSAGE = {
    "id": "m1",
    "threadId": "t1",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "Hi there",
    "historyId": "999",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "subject", "value": "Invoice"},
            {"name": "X-Spam", "value": "no"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}
                        ],
                        "body": {"data": _b64("Hello café, see attached."), "size": 25},
                    },
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Hello</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "att-1", "size": 12345},
            },
        ],
    },
}


def test_search_lists_then_fetches_metadata_and_clamps() -> None:
    ctx = (
        FakeContext()
        .respond({"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "p2"})
        .respond(
            {
                "id": "m1",
                "threadId": "t1",
                "snippet": "Hi",
                "payload": {"headers": [{"name": "Subject", "value": "Invoice"}]},
            }
        )
    )
    out = call(
        registry_for(ADAPTER, ctx), "gmail_search_messages", query="from:alice", max_results=500
    )

    assert out == {
        "messages": [{"id": "m1", "threadId": "t1", "snippet": "Hi", "subject": "Invoice"}],
        "next_page_token": "p2",
        "result_size_estimate": None,
    }
    list_req, get_req = ctx.requests
    assert list_req.method == "GET"
    assert "/gmail/v1/users/me/messages?" in list_req.uri
    q = _query(list_req.uri)
    assert q["q"] == ["from:alice"]
    assert q["maxResults"] == ["25"]
    assert q["includeSpamTrash"] == ["false"]
    gq = _query(get_req.uri)
    assert gq["format"] == ["metadata"]
    assert set(gq["metadataHeaders"]) == {"From", "To", "Cc", "Subject", "Date"}


def test_search_without_headers_makes_one_request() -> None:
    ctx = FakeContext().respond({"messages": [{"id": "m1", "threadId": "t1"}]})
    out = call(registry_for(ADAPTER, ctx), "gmail_search_messages", include_headers=False)
    assert out["messages"] == [{"id": "m1", "threadId": "t1"}]
    assert len(ctx.requests) == 1
    assert "q" not in _query(ctx.requests[0].uri)


def test_get_message_decodes_plain_text_and_lists_attachments_without_bytes() -> None:
    ctx = FakeContext().respond(FULL_MESSAGE)
    out = call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="m1")

    assert "/users/me/messages/m1?" in ctx.requests[0].uri
    assert _query(ctx.requests[0].uri)["format"] == ["full"]
    assert out["headers"] == {"From": "Alice <alice@example.com>", "Subject": "Invoice"}
    assert out["body"] == "Hello café, see attached."
    assert out["body_source"] == "text/plain"
    assert out["body_truncated"] is False
    assert out["attachments"] == [
        {
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size": 12345,
            "attachment_id": "att-1",
        }
    ]
    assert "historyId" not in out
    assert "payload" not in out


def test_get_message_truncates_body() -> None:
    ctx = FakeContext().respond(FULL_MESSAGE)
    out = call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="m1", max_body_chars=5)
    assert out["body"] == "Hello"
    assert out["body_truncated"] is True
    assert out["body_chars"] == len("Hello café, see attached.")


def test_get_message_falls_back_to_html() -> None:
    message = {
        "id": "m2",
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64("<style>p{}</style><p>Line one</p><p>Line &amp; two</p>")},
        },
    }
    ctx = FakeContext().respond(message)
    out = call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="m2")
    assert out["body"] == "Line one\n\nLine & two"
    assert out["body_source"] == "text/html"


def test_html_to_text_skips_scripts() -> None:
    assert html_to_text("<script>alert(1)</script>Hi<br>there") == "Hi\nthere"


def test_get_thread_shapes_each_message() -> None:
    ctx = FakeContext().respond({"id": "t1", "historyId": "1", "messages": [FULL_MESSAGE]})
    out = call(registry_for(ADAPTER, ctx), "gmail_get_thread", thread_id="t1", max_body_chars=3)
    assert "/users/me/threads/t1?" in ctx.requests[0].uri
    assert out["id"] == "t1"
    assert out["messages"][0]["body"] == "Hel"


def test_get_thread_caps_total_body_size() -> None:
    long_message = {
        "id": "m",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64("x" * 30_000)}},
    }
    ctx = FakeContext().respond({"id": "t1", "messages": [long_message] * 100})
    out = call(
        registry_for(ADAPTER, ctx), "gmail_get_thread", thread_id="t1", max_body_chars=100_000
    )
    lengths = [len(m["body"]) for m in out["messages"]]
    assert len(lengths) == 100
    assert sum(lengths) == MAX_THREAD_CHARS
    assert lengths[:3] == [30_000, 10_000, 0]
    assert out["bodies_truncated"] is True
    assert all(m["body_truncated"] for m in out["messages"][1:])


def test_list_labels_trims_fields() -> None:
    ctx = FakeContext().respond(
        {
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system", "messageListVisibility": "x"}
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "gmail_list_labels")
    assert out == {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]}


def test_create_draft_builds_raw_mime() -> None:
    ctx = FakeContext().respond(
        {"id": "d1", "message": {"id": "m9", "threadId": "t1", "labelIds": ["DRAFT"]}}
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "gmail_create_draft",
        to=["bob@example.com", "carol@example.com"],
        subject="Re: Invoice",
        body="Thanks!",
        cc=["dan@example.com"],
        thread_id="t1",
        in_reply_to="<abc@example.com>",
    )

    assert out == {"draft_id": "d1", "message": {"id": "m9", "threadId": "t1"}}
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "/gmail/v1/users/me/drafts" in req.uri
    assert req.body["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(req.body["message"]["raw"])
    mime = message_from_bytes(raw)
    assert mime["To"] == "bob@example.com, carol@example.com"
    assert mime["Cc"] == "dan@example.com"
    assert mime["Subject"] == "Re: Invoice"
    assert mime["In-Reply-To"] == "<abc@example.com>"
    assert parsedate_to_datetime(mime["Date"]).tzinfo is not None
    assert mime["From"] is None
    assert mime.get_payload().strip() == "Thanks!"


def test_header_injection_is_rejected_before_any_request() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(
            reg,
            "gmail_create_draft",
            to=["a@example.com"],
            subject="x\nBcc: evil@example.com",
            body="b",
        )
    with pytest.raises(GoogleMCPError):
        call(
            reg,
            "gmail_create_draft",
            to=["a@example.com\r\nBcc: e@example.com"],
            subject="x",
            body="b",
        )
    with pytest.raises(GoogleMCPError):
        call(reg, "gmail_send_message", to=[], subject="x", body="b")
    assert ctx.requests == []


def test_send_message_posts_raw() -> None:
    ctx = FakeContext().respond({"id": "m3", "threadId": "t3", "labelIds": ["SENT"]})
    out = call(
        registry_for(ADAPTER, ctx),
        "gmail_send_message",
        to=["bob@example.com"],
        subject="Hi",
        body="Yo",
    )
    assert out == {"id": "m3", "threadId": "t3", "labelIds": ["SENT"]}
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "/users/me/messages/send" in req.uri
    assert "threadId" not in req.body
    mime = message_from_bytes(base64.urlsafe_b64decode(req.body["raw"]))
    assert mime["To"] == "bob@example.com"
    assert parsedate_to_datetime(mime["Date"]).tzinfo is not None


def test_send_draft_posts_id() -> None:
    ctx = FakeContext().respond({"id": "m4", "threadId": "t4"})
    call(registry_for(ADAPTER, ctx), "gmail_send_draft", draft_id="d1")
    assert ctx.requests[0].method == "POST"
    assert "/users/me/drafts/send" in ctx.requests[0].uri
    assert ctx.requests[0].body == {"id": "d1"}


def test_modify_labels_sends_body_and_refuses_trash() -> None:
    ctx = FakeContext().respond({"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]})
    reg = registry_for(ADAPTER, ctx)
    call(reg, "gmail_modify_message_labels", message_id="m1", remove_label_ids=["UNREAD"])
    assert "/users/me/messages/m1/modify" in ctx.requests[0].uri
    assert ctx.requests[0].body == {"addLabelIds": [], "removeLabelIds": ["UNREAD"]}

    with pytest.raises(GoogleMCPError, match="TRASH"):
        call(reg, "gmail_modify_message_labels", message_id="m1", add_label_ids=["TRASH"])
    with pytest.raises(GoogleMCPError):
        call(reg, "gmail_modify_message_labels", message_id="m1")
    assert len(ctx.requests) == 1


def test_tool_flags() -> None:
    reads = ["gmail_search_messages", "gmail_get_message", "gmail_get_thread", "gmail_list_labels"]
    for name in reads:
        assert not _tool(name).write
    assert _tool("gmail_create_draft").write and not _tool("gmail_create_draft").destructive
    assert _tool("gmail_modify_message_labels").write
    assert not _tool("gmail_modify_message_labels").destructive
    assert _tool("gmail_send_draft").destructive
    assert _tool("gmail_send_message").destructive


def test_scopes_are_least_privilege() -> None:
    assert ADAPTER.spec.read_scopes == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert ADAPTER.spec.write_scopes == ("https://www.googleapis.com/auth/gmail.modify",)


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 404, "message": "Not Found"}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="missing")
    assert info.value.status == 404


def _out_of_line(size: int) -> dict[str, object]:
    return {
        "id": "m7",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"attachmentId": "body-1", "size": size},
                }
            ],
        },
    }


def test_out_of_line_text_body_is_fetched() -> None:
    ctx = (
        FakeContext()
        .respond(_out_of_line(40))
        .respond({"size": 40, "data": _b64("Large body text")})
    )
    out = call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="m7")
    assert out["body"] == "Large body text"
    assert out["body_truncated"] is False
    assert out["attachments"] == []
    fetch = ctx.requests[1]
    assert fetch.method == "GET"
    assert "/users/me/messages/m7/attachments/body-1" in fetch.uri


def test_oversized_out_of_line_body_is_flagged_not_fetched() -> None:
    ctx = FakeContext().respond(_out_of_line(MAX_BODY_FETCH_BYTES + 1))
    out = call(registry_for(ADAPTER, ctx), "gmail_get_message", message_id="m7")
    assert out["body"] == ""
    assert out["body_truncated"] is True
    assert out["body_parts_not_fetched"] == 1
    assert len(ctx.requests) == 1


def test_thread_fetches_out_of_line_body_with_message_id() -> None:
    ctx = (
        FakeContext()
        .respond({"id": "t7", "messages": [_out_of_line(10)]})
        .respond({"data": _b64("hello")})
    )
    out = call(registry_for(ADAPTER, ctx), "gmail_get_thread", thread_id="t7")
    assert out["messages"][0]["body"] == "hello"
    assert "/messages/m7/attachments/body-1" in ctx.requests[1].uri
