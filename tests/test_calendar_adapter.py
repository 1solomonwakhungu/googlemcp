from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.calendar import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def _tool(name: str):  # type: ignore[no-untyped-def]
    return next(t for t in registry_for(ADAPTER, FakeContext()).tools if t.name == name)


EVENT = {
    "id": "e1",
    "etag": "x",
    "status": "confirmed",
    "summary": "Standup",
    "description": "d" * 5000,
    "start": {"dateTime": "2026-10-06T09:00:00-04:00"},
    "end": {"dateTime": "2026-10-06T09:15:00-04:00"},
    "organizer": {"email": "me@example.com", "self": True, "id": "zzz"},
    "attendees": [{"email": "bob@example.com", "responseStatus": "accepted", "id": "q"}],
    "iCalUID": "drop-me",
}


def test_list_calendars_trims_and_clamps() -> None:
    ctx = FakeContext().respond(
        {"items": [{"id": "primary@example.com", "summary": "Me", "primary": True, "etag": "e"}]}
    )
    out = call(registry_for(ADAPTER, ctx), "calendar_list_calendars", max_results=1000)
    assert out == {
        "calendars": [{"id": "primary@example.com", "summary": "Me", "primary": True}],
        "next_page_token": None,
    }
    assert "/calendar/v3/users/me/calendarList" in ctx.requests[0].uri
    assert _query(ctx.requests[0].uri)["maxResults"] == ["100"]


def test_list_events_builds_time_range_query_and_shapes() -> None:
    ctx = FakeContext().respond(
        {"items": [EVENT], "timeZone": "America/New_York", "nextPageToken": "n"}
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "calendar_list_events",
        time_min="2026-10-06T00:00:00Z",
        time_max="2026-10-07T00:00:00Z",
        query="standup",
    )
    req = ctx.requests[0]
    assert req.method == "GET"
    assert "/calendar/v3/calendars/primary/events" in req.uri
    q = _query(req.uri)
    assert q["timeMin"] == ["2026-10-06T00:00:00Z"]
    assert q["timeMax"] == ["2026-10-07T00:00:00Z"]
    assert q["q"] == ["standup"]
    assert q["singleEvents"] == ["true"]
    assert q["orderBy"] == ["startTime"]

    event = out["events"][0]
    assert out["next_page_token"] == "n"
    assert event["organizer"] == {"email": "me@example.com", "self": True}
    assert event["attendees"] == [{"email": "bob@example.com", "responseStatus": "accepted"}]
    assert len(event["description"]) == 4000
    assert event["description_truncated"] is True
    assert "etag" not in event and "iCalUID" not in event


def test_list_events_without_expansion_omits_order_by() -> None:
    ctx = FakeContext().respond({"items": []})
    call(registry_for(ADAPTER, ctx), "calendar_list_events", single_events=False)
    q = _query(ctx.requests[0].uri)
    assert "orderBy" not in q
    assert q["singleEvents"] == ["false"]


def test_list_events_rejects_naive_timestamps() -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError, match="UTC offset"):
        call(registry_for(ADAPTER, ctx), "calendar_list_events", time_min="2026-10-06T00:00:00")
    assert ctx.requests == []


def test_get_event_encodes_ids() -> None:
    ctx = FakeContext().respond(EVENT)
    call(
        registry_for(ADAPTER, ctx),
        "calendar_get_event",
        event_id="e1",
        calendar_id="team@group.calendar.google.com",
    )
    assert "/calendars/team%40group.calendar.google.com/events/e1" in ctx.requests[0].uri


def test_freebusy_posts_body() -> None:
    ctx = FakeContext().respond(
        {
            "kind": "calendar#freeBusy",
            "timeMin": "2026-10-06T00:00:00.000Z",
            "timeMax": "2026-10-07T00:00:00.000Z",
            "calendars": {"primary": {"busy": [{"start": "a", "end": "b"}]}},
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "calendar_query_freebusy",
        time_min="2026-10-06T00:00:00Z",
        time_max="2026-10-07T00:00:00Z",
        time_zone="UTC",
    )
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.uri.split("?")[0].endswith("/calendar/v3/freeBusy")
    assert req.body == {
        "timeMin": "2026-10-06T00:00:00Z",
        "timeMax": "2026-10-07T00:00:00Z",
        "items": [{"id": "primary"}],
        "timeZone": "UTC",
    }
    assert out["calendars"] == {"primary": {"busy": [{"start": "a", "end": "b"}]}}


def test_create_event_defaults_send_updates_none() -> None:
    ctx = FakeContext().respond(EVENT)
    call(
        registry_for(ADAPTER, ctx),
        "calendar_create_event",
        summary="Standup",
        start="2026-10-06T09:00:00",
        end="2026-10-06T09:15:00",
        time_zone="America/New_York",
        attendees=["bob@example.com"],
    )
    req = ctx.requests[0]
    assert req.method == "POST"
    assert _query(req.uri)["sendUpdates"] == ["none"]
    assert req.body == {
        "summary": "Standup",
        "start": {"dateTime": "2026-10-06T09:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-06T09:15:00", "timeZone": "America/New_York"},
        "attendees": [{"email": "bob@example.com"}],
    }


def test_create_all_day_event_uses_date() -> None:
    ctx = FakeContext().respond({"id": "e2"})
    call(
        registry_for(ADAPTER, ctx),
        "calendar_create_event",
        summary="Off",
        start="2026-10-06",
        end="2026-10-07",
    )
    assert ctx.requests[0].body["start"] == {"date": "2026-10-06"}
    assert ctx.requests[0].body["end"] == {"date": "2026-10-07"}


def test_create_event_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError, match="both"):
        call(
            reg,
            "calendar_create_event",
            summary="x",
            start="2026-10-06",
            end="2026-10-06T10:00:00Z",
        )
    with pytest.raises(GoogleMCPError, match="send_updates"):
        call(
            reg,
            "calendar_create_event",
            summary="x",
            start="2026-10-06",
            end="2026-10-07",
            send_updates="everyone",
        )
    with pytest.raises(GoogleMCPError):
        call(reg, "calendar_create_event", summary="x", start="tomorrow", end="2026-10-07")
    assert ctx.requests == []


def test_update_event_patches_only_given_fields() -> None:
    ctx = FakeContext().respond(EVENT)
    call(
        registry_for(ADAPTER, ctx),
        "calendar_update_event",
        event_id="e1",
        summary="New",
        send_updates="all",
    )
    req = ctx.requests[0]
    assert req.method == "PATCH"
    assert "/calendars/primary/events/e1" in req.uri
    assert req.body == {"summary": "New"}
    assert _query(req.uri)["sendUpdates"] == ["all"]

    with pytest.raises(GoogleMCPError, match="nothing"):
        call(registry_for(ADAPTER, FakeContext()), "calendar_update_event", event_id="e1")


def test_delete_event() -> None:
    ctx = FakeContext().respond("", status=204)
    out = call(registry_for(ADAPTER, ctx), "calendar_delete_event", event_id="e1")
    assert out == {"deleted": True, "event_id": "e1", "calendar_id": "primary"}
    assert ctx.requests[0].method == "DELETE"
    assert _query(ctx.requests[0].uri)["sendUpdates"] == ["none"]


def test_tool_flags_and_scopes() -> None:
    for name in (
        "calendar_list_calendars",
        "calendar_list_events",
        "calendar_get_event",
        "calendar_query_freebusy",
    ):
        assert not _tool(name).write
    assert _tool("calendar_create_event").write and not _tool("calendar_create_event").destructive
    assert _tool("calendar_update_event").write and not _tool("calendar_update_event").destructive
    assert _tool("calendar_delete_event").destructive
    assert "https://www.googleapis.com/auth/calendar.readonly" not in ADAPTER.spec.read_scopes
    assert ADAPTER.spec.write_scopes == ("https://www.googleapis.com/auth/calendar.events",)


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 403, "message": "Forbidden"}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "calendar_get_event", event_id="e1")
    assert info.value.status == 403


@pytest.mark.parametrize(
    "value",
    [
        "2026-W41-2",
        "20261005",
        "2026-10-05T09:00",
        "2026-10-05 09:00:00Z",
        "2026-W41-2T09:00:00Z",
        "2026-10-05T25:00:00Z",
        "2026-02-30",
    ],
)
def test_event_times_are_strict_rfc3339_or_date(value: str) -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(
            registry_for(ADAPTER, ctx),
            "calendar_create_event",
            summary="x",
            start=value,
            end=value,
            time_zone="UTC",
        )
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, ctx), "calendar_list_events", time_min=value)
    assert ctx.requests == []


def test_fractional_seconds_and_offsets_are_accepted() -> None:
    ctx = FakeContext().respond({"items": []})
    call(
        registry_for(ADAPTER, ctx),
        "calendar_list_events",
        time_min="2026-10-05T09:00:00.250+02:00",
    )
    assert _query(ctx.requests[0].uri)["timeMin"] == ["2026-10-05T09:00:00.250+02:00"]
