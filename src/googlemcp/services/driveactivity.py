"""Google Drive Activity adapter.

Scope choices (per-method scopes from the Drive Activity v2 discovery
document, https://developers.google.com/workspace/drive/activity/v2/guides/authorizing):

- Read: ``drive.activity.readonly``, the narrower of the two scopes that
  ``activity.query`` accepts. The API is read-only, so there are no write scopes.

Actors are returned as People API resource names (``people/...``); resolve
them with the people adapter if names are needed.
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
    key="driveactivity",
    title="Drive Activity",
    api_services=("driveactivity.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/drive.activity.readonly",),
)

MAX_DETAIL_CHARS = 2_000


def _item_name(value: str) -> str:
    return value if value.startswith("items/") else f"items/{value}"


def _only_key(obj: JSON) -> str | None:
    return next(iter(obj), None) if obj else None


def _drive_item(item: JSON) -> JSON:
    out = pick(item, ("name", "title", "mimeType"))
    if "driveFolder" in item:
        out["kind"] = "folder"
    elif "driveFile" in item:
        out["kind"] = "file"
    return out


def shape_actor(actor: JSON) -> JSON:
    user = actor.get("user")
    if user and "knownUser" in user:
        known = user["knownUser"]
        return {
            "type": "user",
            "person": known.get("personName"),
            "is_current_user": bool(known.get("isCurrentUser", False)),
        }
    if user:
        return {"type": _only_key(user) or "user"}
    return {"type": _only_key(actor) or "unknown"}


def shape_target(target: JSON) -> JSON:
    if "driveItem" in target:
        return {"type": "drive_item", **_drive_item(target["driveItem"])}
    if "drive" in target:
        return {"type": "shared_drive", **pick(target["drive"], ("name", "title"))}
    if "fileComment" in target:
        comment = target["fileComment"]
        out: JSON = {"type": "comment", **pick(comment, ("linkToDiscussion",))}
        if "parent" in comment:
            out["parent"] = _drive_item(comment["parent"])
        return out
    return {"type": _only_key(target) or "unknown"}


def shape_activity(activity: JSON) -> JSON:
    primary = activity.get("primaryActionDetail", {}) or {}
    action = _only_key(primary)
    out: JSON = {"action": action}
    detail = primary.get(action) if action else None
    if detail:
        # Keep small, readable details (rename titles, move parents); drop huge ones.
        text = str(detail)
        out["detail"] = detail if len(text) <= MAX_DETAIL_CHARS else {"truncated": True}
    if "timestamp" in activity:
        out["timestamp"] = activity["timestamp"]
    elif "timeRange" in activity:
        out["time_range"] = pick(activity["timeRange"], ("startTime", "endTime"))
    out["actors"] = [shape_actor(a) for a in activity.get("actors", [])]
    out["targets"] = [shape_target(t) for t in activity.get("targets", [])]
    return out


class DriveActivityAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("driveactivity_query_activity")
        def query_activity(
            item_id: str | None = None,
            folder_id: str | None = None,
            filter: str | None = None,
            consolidate: bool = False,
            max_results: int = 25,
            page_token: str | None = None,
        ) -> JSON:
            """Query Drive activity, newest first, for one file (item_id), everything under a
            folder (folder_id), or the whole Drive (neither).

            filter uses the Drive Activity syntax, for example
            'time >= "2026-10-01T00:00:00Z" AND detail.action_detail_case:(EDIT RENAME)'.
            consolidate=true groups related actions.
            """
            if item_id and folder_id:
                raise GoogleMCPError("pass item_id or folder_id, not both")
            body: JSON = {"pageSize": clamp_page_size(max_results)}
            if item_id:
                body["itemName"] = _item_name(item_id)
            if folder_id:
                body["ancestorName"] = _item_name(folder_id)
            if filter:
                body["filter"] = filter
            if page_token:
                body["pageToken"] = page_token
            body["consolidationStrategy"] = {"legacy": {}} if consolidate else {"none": {}}
            svc = ctx.client("driveactivity", "v2")
            resp = ctx.execute(svc.activity().query(body=body))
            return {
                "activities": [shape_activity(a) for a in resp.get("activities", [])],
                "next_page_token": resp.get("nextPageToken"),
            }


ADAPTER = DriveActivityAdapter()
