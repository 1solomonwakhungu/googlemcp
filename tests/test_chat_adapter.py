from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.chat import ADAPTER, MAX_MESSAGE_CHARS, MAX_SEND_BYTES
from googlemcp.testing import FakeContext, call, registry_for


def test_list_spaces_with_type_filter() -> None:
    ctx = FakeContext().respond(
        {
            "spaces": [
                {"name": "spaces/A", "displayName": "Team", "spaceType": "SPACE", "spaceUri": "u"}
            ],
            "nextPageToken": "n",
        }
    )
    out = call(registry_for(ADAPTER, ctx), "chat_list_spaces", space_type="SPACE", page_size=1000)
    assert out == {
        "spaces": [{"name": "spaces/A", "displayName": "Team", "spaceType": "SPACE"}],
        "next_page_token": "n",
    }
    q = parse_qs(urlparse(ctx.requests[0].uri).query)
    assert q["filter"] == ['space_type = "SPACE"']
    assert q["pageSize"] == ["100"]


def test_list_messages_orders_and_caps_text() -> None:
    ctx = FakeContext().respond(
        {
            "messages": [
                {
                    "name": "spaces/A/messages/m1",
                    "sender": {"name": "users/1", "type": "HUMAN"},
                    "createTime": "t",
                    "thread": {"name": "spaces/A/threads/t1"},
                    "text": "y" * (MAX_MESSAGE_CHARS + 1),
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "chat_list_messages", space="A")
    msg = out["messages"][0]
    assert msg["truncated"] is True and len(msg["text"]) == MAX_MESSAGE_CHARS
    assert msg["sender"] == "users/1" and msg["thread"] == "spaces/A/threads/t1"
    assert urlparse(ctx.requests[0].uri).path == "/v1/spaces/A/messages"
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["orderBy"] == ["create_time DESC"]


def test_create_message_in_thread() -> None:
    ctx = FakeContext().respond(
        {"name": "spaces/A/messages/m2", "text": "hi", "thread": {"name": "t"}}
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "chat_create_message",
        space="spaces/A",
        text="hi",
        thread_key="k1",
    )
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.body == {"text": "hi", "thread": {"threadKey": "k1"}}
    q = parse_qs(urlparse(req.uri).query)
    assert q["messageReplyOption"] == ["REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"]
    assert out["name"] == "spaces/A/messages/m2"


def test_create_message_is_write_and_destructive() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    flags = {t.name: (t.write, t.destructive) for t in reg.tools}
    assert flags == {
        "chat_list_spaces": (False, False),
        "chat_list_messages": (False, False),
        "chat_create_message": (True, True),
    }


def test_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "chat_create_message", space="A", text="  ")
    with pytest.raises(GoogleMCPError):
        call(reg, "chat_create_message", space="A", text="x" * (MAX_SEND_BYTES + 1))
    with pytest.raises(GoogleMCPError):
        call(reg, "chat_list_messages", space="spaces/A/messages/m1")
    with pytest.raises(GoogleMCPError):
        call(reg, "chat_list_spaces", space_type="ROOM")
    assert ctx.requests == []


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "chat_list_spaces")
    assert info.value.status == 403


def test_list_messages_oldest_first() -> None:
    ctx = FakeContext().respond({"messages": []})
    call(registry_for(ADAPTER, ctx), "chat_list_messages", space="A", newest_first=False)
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["orderBy"] == ["create_time ASC"]
