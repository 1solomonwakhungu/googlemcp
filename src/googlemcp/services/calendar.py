"""Google Calendar adapter.

Scope choices (per-method scopes from the Calendar v3 discovery document,
https://developers.google.com/workspace/calendar/api/auth):

- Read: three granular scopes instead of ``calendar.readonly``, which would
  also expose calendar settings and ACLs.
  ``calendar.calendarlist.readonly`` for ``calendarList.list``,
  ``calendar.events.readonly`` for ``events.list`` and ``events.get``,
  ``calendar.freebusy`` for ``freebusy.query``.
- Write: ``calendar.events`` for ``events.insert``, ``events.patch`` and
  ``events.delete``. It cannot change calendars, sharing, or settings.

Write tools default ``send_updates`` to ``"none"`` so attendees are not
emailed unless the caller asks for it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

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
    key="calendar",
    title="Google Calendar",
    api_services=("calendar-json.googleapis.com",),
    read_scopes=(
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.freebusy",
    ),
    write_scopes=("https://www.googleapis.com/auth/calendar.events",),
)

SEND_UPDATES = frozenset({"all", "externalOnly", "none"})
MAX_DESCRIPTION_CHARS = 4_000
MAX_FREEBUSY_CALENDARS = 50
_CALENDAR_FIELDS = ("id", "summary", "description", "timeZone", "primary", "accessRole")
_EVENT_FIELDS = (
    "id",
    "status",
    "summary",
    "location",
    "start",
    "end",
    "htmlLink",
    "hangoutLink",
    "recurringEventId",
    "recurrence",
    "created",
    "updated",
    "transparency",
    "visibility",
)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# RFC 3339 date-time; the offset group is optional here and enforced by callers.
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?(?P<offset>[Zz]|[+-]\d{2}:\d{2})?$"
)


def _parse_datetime(field: str, value: str) -> bool:
    """Validate an RFC 3339 date-time strictly; return whether it has an offset.

    ``datetime.fromisoformat`` alone also accepts ISO 8601 forms Google rejects
    (week dates, basic format, missing seconds), so the shape is checked first.
    """
    match = _DATETIME_RE.match(value)
    if not match:
        raise GoogleMCPError(f"{field}: expected RFC 3339 like 2026-10-05T09:00:00Z, got {value!r}")
    try:
        datetime.fromisoformat(value.upper())
    except ValueError as exc:
        raise GoogleMCPError(f"{field}: invalid date-time {value!r}") from exc
    return match.group("offset") is not None


def _require_timestamp(field: str, value: str) -> str:
    """Accept RFC 3339 timestamps with an explicit offset, as Google requires."""
    if not _parse_datetime(field, value):
        raise GoogleMCPError(f"{field}: include a UTC offset, for example 2026-10-05T09:00:00Z")
    return value


def event_time(field: str, value: str, time_zone: str | None) -> JSON:
    """Map YYYY-MM-DD to an all-day ``date`` and an RFC 3339 date-time to ``dateTime``."""
    if _DATE_RE.match(value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise GoogleMCPError(f"{field}: invalid date {value!r}") from exc
        return {"date": value}
    if not _parse_datetime(field, value) and not time_zone:
        raise GoogleMCPError(f"{field}: include a UTC offset or pass time_zone")
    out: JSON = {"dateTime": value}
    if time_zone:
        out["timeZone"] = time_zone
    return out


def _send_updates(value: str) -> str:
    if value not in SEND_UPDATES:
        raise GoogleMCPError(f"send_updates must be one of {sorted(SEND_UPDATES)}")
    return value


def shape_event(event: JSON) -> JSON:
    out = pick(event, _EVENT_FIELDS)
    description = event.get("description")
    if description:
        out["description"] = description[:MAX_DESCRIPTION_CHARS]
        out["description_truncated"] = len(description) > MAX_DESCRIPTION_CHARS
    if "organizer" in event:
        out["organizer"] = pick(event["organizer"], ("email", "displayName", "self"))
    if "attendees" in event:
        out["attendees"] = [
            pick(a, ("email", "displayName", "responseStatus", "optional", "self"))
            for a in event["attendees"]
        ]
    return out


def _event_body(
    *,
    summary: str | None,
    start: str | None,
    end: str | None,
    description: str | None,
    location: str | None,
    attendees: list[str] | None,
    time_zone: str | None,
) -> JSON:
    body: JSON = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        body["start"] = event_time("start", start, time_zone)
    if end is not None:
        body["end"] = event_time("end", end, time_zone)
    if attendees is not None:
        body["attendees"] = [{"email": email} for email in attendees]
    if "start" in body and "end" in body and ("date" in body["start"]) != ("date" in body["end"]):
        raise GoogleMCPError("start and end must both be dates or both be timestamps")
    return body


class CalendarAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        def svc() -> Any:
            return ctx.client("calendar", "v3")

        @registry.tool("calendar_list_calendars")
        def list_calendars(max_results: int = 50, page_token: str | None = None) -> JSON:
            """List calendars on the user's calendar list, including 'primary'."""
            resp = ctx.execute(
                svc()
                .calendarList()
                .list(maxResults=clamp_page_size(max_results), pageToken=page_token)
            )
            return {
                "calendars": [pick(i, _CALENDAR_FIELDS) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("calendar_list_events")
        def list_events(
            calendar_id: str = "primary",
            time_min: str | None = None,
            time_max: str | None = None,
            query: str | None = None,
            max_results: int = 25,
            page_token: str | None = None,
            single_events: bool = True,
        ) -> JSON:
            """List events in a time range.

            Times are RFC 3339 with an offset, for example 2026-10-05T00:00:00Z.

            single_events=true expands recurring events and orders by start time.
            """
            resp = ctx.execute(
                svc()
                .events()
                .list(
                    calendarId=calendar_id,
                    timeMin=_require_timestamp("time_min", time_min) if time_min else None,
                    timeMax=_require_timestamp("time_max", time_max) if time_max else None,
                    q=query or None,
                    maxResults=clamp_page_size(max_results),
                    pageToken=page_token,
                    singleEvents=single_events,
                    orderBy="startTime" if single_events else None,
                )
            )
            return {
                "events": [shape_event(i) for i in resp.get("items", [])],
                "time_zone": resp.get("timeZone"),
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("calendar_get_event")
        def get_event(event_id: str, calendar_id: str = "primary") -> JSON:
            """Get one event by id."""
            resp = ctx.execute(svc().events().get(calendarId=calendar_id, eventId=event_id))
            return shape_event(resp)

        @registry.tool("calendar_query_freebusy")
        def query_freebusy(
            time_min: str,
            time_max: str,
            calendar_ids: list[str] | None = None,
            time_zone: str | None = None,
        ) -> JSON:
            """Return busy intervals for calendars (default: primary) between two RFC 3339 times."""
            ids = calendar_ids or ["primary"]
            if len(ids) > MAX_FREEBUSY_CALENDARS:
                raise GoogleMCPError(f"at most {MAX_FREEBUSY_CALENDARS} calendars per query")
            body: JSON = {
                "timeMin": _require_timestamp("time_min", time_min),
                "timeMax": _require_timestamp("time_max", time_max),
                "items": [{"id": i} for i in ids],
            }
            if time_zone:
                body["timeZone"] = time_zone
            resp = ctx.execute(svc().freebusy().query(body=body))
            return {
                "time_min": resp.get("timeMin"),
                "time_max": resp.get("timeMax"),
                "calendars": {
                    cal_id: pick(info, ("busy", "errors"))
                    for cal_id, info in resp.get("calendars", {}).items()
                },
            }

        @registry.tool("calendar_create_event", write=True)
        def create_event(
            summary: str,
            start: str,
            end: str,
            calendar_id: str = "primary",
            description: str | None = None,
            location: str | None = None,
            attendees: list[str] | None = None,
            time_zone: str | None = None,
            send_updates: str = "none",
        ) -> JSON:
            """Create an event. start/end are RFC 3339 timestamps, or YYYY-MM-DD for all-day events
            (end date is exclusive). send_updates is 'none' (default), 'all', or 'externalOnly'.
            """
            body = _event_body(
                summary=summary,
                start=start,
                end=end,
                description=description,
                location=location,
                attendees=attendees,
                time_zone=time_zone,
            )
            resp = ctx.execute(
                svc()
                .events()
                .insert(calendarId=calendar_id, body=body, sendUpdates=_send_updates(send_updates))
            )
            return shape_event(resp)

        @registry.tool("calendar_update_event", write=True, idempotent=True)
        def update_event(
            event_id: str,
            calendar_id: str = "primary",
            summary: str | None = None,
            start: str | None = None,
            end: str | None = None,
            description: str | None = None,
            location: str | None = None,
            attendees: list[str] | None = None,
            time_zone: str | None = None,
            send_updates: str = "none",
        ) -> JSON:
            """Patch an event. Only fields you pass change; attendees replaces the whole list."""
            body = _event_body(
                summary=summary,
                start=start,
                end=end,
                description=description,
                location=location,
                attendees=attendees,
                time_zone=time_zone,
            )
            if not body:
                raise GoogleMCPError("nothing to update")
            resp = ctx.execute(
                svc()
                .events()
                .patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=body,
                    sendUpdates=_send_updates(send_updates),
                )
            )
            return shape_event(resp)

        @registry.tool("calendar_delete_event", write=True, destructive=True, idempotent=True)
        def delete_event(
            event_id: str, calendar_id: str = "primary", send_updates: str = "none"
        ) -> JSON:
            """Delete an event. send_updates controls whether attendees are notified."""
            ctx.execute(
                svc()
                .events()
                .delete(
                    calendarId=calendar_id,
                    eventId=event_id,
                    sendUpdates=_send_updates(send_updates),
                )
            )
            return {"deleted": True, "event_id": event_id, "calendar_id": calendar_id}


ADAPTER = CalendarAdapter()
