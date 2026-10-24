"""Read-only product probes for ``googlemcp doctor --probe`` and live verification.

Each catalog product has one probe: the cheapest read-only call that proves
the API is enabled in the Google Cloud project and that the stored token is
accepted. Probes never mutate state and never create server-side jobs or
sessions. Every list call asks for at most one item, except two APIs with no
server-side page size: YouTube Analytics ``groups.list`` and Search Console
``sites.list``. Those use a ``fields`` projection so each returned entry is a
single short field. Results carry fixed, sanitized detail strings; Google
error text, tokens, and account data are never copied.

Classification (see ``classify``), checked in order:

1. HTTP 401, a credential refresh failure, or the core's ``AuthError``:
   ``auth_error``.
2. Error reason ``SERVICE_DISABLED`` or ``accessNotConfigured``: ``not_enabled``.
   Apps Script's documented "User has not enabled the Apps Script API" error
   (the per-user setting at script.google.com/home/usersettings) is also
   ``not_enabled``. Setting: https://developers.google.com/apps-script/api/how-tos/enable;
   error text and 403 status as returned to Google's own clasp CLI:
   https://github.com/google/clasp/issues/973.
3. Error reason ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``, ``insufficientPermissions``
   or ``insufficientScopes``: ``permission`` (token lacks a scope).
4. Workspace-only products (Meet, Chat). A generic 403 is not proof of a
   consumer account (Workspace accounts get them too), so:

   - ``unsupported_account`` only when Google's error message contains
     "only available to Google Workspace users", the wording Google
     documents for consumer accounts
     (https://developers.google.com/workspace/chat/troubleshoot-chat-apps).
   - Chat 404 "Google Chat app not found": ``not_enabled``; the Chat app is
     not configured in the console (manual step, any account type).
   - Any other 403: ``permission``, noting a consumer account may be the cause.

   Google error text is only matched here, never copied into ``detail``.
5. Not-found probes (Docs, Sheets, Slides, Forms, Apps Script): these
   probes read an id that cannot exist, because the API has no list call or
   its list call needs a scope the adapter does not request (Apps Script
   ``processes.list`` needs ``script.processes``). Only
   HTTP 404 (NOT_FOUND) counts as ``ok``: Google checks enablement and
   token scopes before the backend looks the id up, so a 404 proves both.
   Any 403, including a generic PERMISSION_DENIED, is not proof and falls
   through to rule 6; a 400 is ``error``.
6. Any other 403: ``permission``. Everything else: ``error``.

Request methods: every probe is a GET except Drive Activity, whose only
endpoint is ``activity.query`` (a read-only POST with ``pageSize=1``).

Quota: YouTube Data ``channels.list(part="id", maxResults=1)`` costs 1 unit.

Photos: the Library API has no static discovery document, so its probe uses
``ctx.authorized_session()``. The Photos Picker API is not probed: its only
entry point, ``sessions.create``, creates a server-side session
(https://developers.google.com/photos/picker/reference/rest).

Analytics: the Data API probe reads ``properties/0/metadata``; property id 0
returns dimensions and metrics common to all properties
(https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/getMetadata).

Classification depends on the core ``ServiceContext.execute`` either setting
``UpstreamError.reason`` or chaining the ``HttpError`` as ``__cause__``;
without either, a disabled API or missing scope is reported as a generic
``permission`` or ``error`` instead of the specific status.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from google.auth.exceptions import DefaultCredentialsError, RefreshError
from googleapiclient.errors import HttpError

from googlemcp.catalog import load_catalog
from googlemcp.services.base import ServiceContext, UpstreamError

STATUSES = frozenset(
    {"ok", "skipped", "auth_error", "not_enabled", "permission", "unsupported_account", "error"}
)
# A document id that cannot exist, for APIs that have no read-only list call.
MISSING_ID = "googlemcp-probe-nonexistent-id"
PHOTOS_ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
HTTP_TIMEOUT_SECONDS = 15

_DISABLED = frozenset({"SERVICE_DISABLED", "accessNotConfigured"})
_SCOPE = frozenset(
    {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions", "insufficientScopes"}
)
_APPS_SCRIPT_USER_SETTING_MARKER = "has not enabled the apps script api"
_CONSUMER_MARKER = "only available to google workspace users"
_CHAT_APP_MISSING_MARKER = "google chat app not found"
_WORKSPACE_403_DETAIL = (
    "permission denied; Meet/Chat REST APIs require a Google Workspace account, "
    "which may be the cause"
)
_AUTH_EXCEPTIONS: tuple[type[BaseException], ...] = (RefreshError, DefaultCredentialsError)


def _auth_exceptions() -> tuple[type[BaseException], ...]:
    """Credential failures, including the core's ``googlemcp.auth.AuthError``.

    The core raises ``AuthError`` for HTTP 401 and refresh failures. It is
    imported lazily so probes stay usable without the core installed.
    """
    try:
        auth_error = importlib.import_module("googlemcp.auth").AuthError
    except (ImportError, AttributeError):
        return _AUTH_EXCEPTIONS
    return (*_AUTH_EXCEPTIONS, auth_error)


@dataclass(frozen=True)
class ProbeResult:
    product: str
    ok: bool
    status: str
    detail: str

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


Probe = Callable[[ServiceContext], ProbeResult]


class _HttpFailure(Exception):
    """A non-2xx response from an authorized_session probe."""

    def __init__(self, status: int, content: bytes):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.content = content


def _reasons_from_body(content: bytes | str | None) -> set[str]:
    """Collect error reason tokens from a Google JSON error body."""
    if not content:
        return set()
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return set()
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return set()
    found: set[str] = set()
    if isinstance(error.get("status"), str):
        found.add(error["status"])
    for item in error.get("errors", []) or []:
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            found.add(item["reason"])
    for item in error.get("details", []) or []:
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            found.add(item["reason"])
    return found


def _message_from_body(content: bytes | str | None) -> str:
    try:
        data = json.loads(content or "")
    except (TypeError, ValueError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


def _error_message(exc: BaseException) -> str:
    """Google's error message, for marker matching only; never put it in output."""
    if isinstance(exc, _HttpFailure):
        return _message_from_body(exc.content)
    cause = exc.__cause__ if isinstance(exc, UpstreamError) else exc
    if isinstance(cause, HttpError):
        return _message_from_body(cause.content)
    return ""


def _status_and_reasons(exc: BaseException) -> tuple[int | None, set[str]]:
    if isinstance(exc, _HttpFailure):
        return exc.status, _reasons_from_body(exc.content)
    if isinstance(exc, UpstreamError):
        reasons = {exc.reason} if exc.reason else set()
        cause = exc.__cause__
        if isinstance(cause, HttpError):
            reasons |= _reasons_from_body(cause.content)
        return exc.status, reasons
    if isinstance(exc, HttpError):
        return int(exc.resp.status), _reasons_from_body(exc.content)
    return None, set()


def classify(
    product: str,
    exc: BaseException,
    *,
    not_found_ok: bool = False,
    workspace_only: bool = False,
) -> ProbeResult:
    """Map a probe failure to a sanitized ``ProbeResult``."""
    auth = _auth_exceptions()
    if isinstance(exc, auth) or isinstance(exc.__cause__, auth):
        return ProbeResult(
            product, False, "auth_error", "credentials rejected or refresh failed; reauthorize"
        )
    status, reasons = _status_and_reasons(exc)
    if status is None and isinstance(exc, UpstreamError):
        return ProbeResult(product, False, "error", "request failed before an HTTP response")
    if status is None:
        return ProbeResult(product, False, "error", f"probe failed: {type(exc).__name__}")
    if status == 401:
        return ProbeResult(
            product, False, "auth_error", "HTTP 401: credentials rejected; reauthorize"
        )
    disabled = sorted(reasons & _DISABLED)
    if disabled:
        return ProbeResult(
            product, False, "not_enabled", f"API not enabled in the project ({disabled[0]})"
        )
    if _APPS_SCRIPT_USER_SETTING_MARKER in _error_message(exc).lower():
        return ProbeResult(
            product,
            False,
            "not_enabled",
            "Apps Script API user setting is off (script.google.com/home/usersettings)",
        )
    scope = sorted(reasons & _SCOPE)
    if scope:
        return ProbeResult(
            product, False, "permission", f"token lacks a required scope ({scope[0]})"
        )
    if workspace_only:
        message = _error_message(exc).lower()
        if _CONSUMER_MARKER in message:
            return ProbeResult(
                product,
                False,
                "unsupported_account",
                "this API requires a Google Workspace account (Google rejected this account)",
            )
        if status == 404 and _CHAT_APP_MISSING_MARKER in message:
            return ProbeResult(
                product,
                False,
                "not_enabled",
                "Chat app not configured in the Google Cloud console (Chat API Configuration)",
            )
        if status == 403:
            return ProbeResult(product, False, "permission", _WORKSPACE_403_DETAIL)
    if not_found_ok and status == 404:
        return ProbeResult(product, True, "ok", "API reachable (probe id not found, as expected)")
    if status == 403:
        return ProbeResult(product, False, "permission", "HTTP 403: access denied")
    if status == 429:
        return ProbeResult(product, False, "error", "HTTP 429: rate limited or quota exhausted")
    return ProbeResult(product, False, "error", f"HTTP {status}")


def _probe(
    product: str,
    call: Callable[[ServiceContext], str],
    *,
    not_found_ok: bool = False,
    workspace_only: bool = False,
) -> Probe:
    def run(ctx: ServiceContext) -> ProbeResult:
        try:
            detail = call(ctx)
        except Exception as exc:  # noqa: BLE001 - a probe must report, never raise
            return classify(product, exc, not_found_ok=not_found_ok, workspace_only=workspace_only)
        return ProbeResult(product, True, "ok", detail)

    run.__doc__ = call.__doc__
    return run


# Each call returns an ok detail string or raises. Keep them to one cheap read.


def _tasks(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("tasks", "v1").tasklists().list(maxResults=1))
    return "task lists readable"


def _gmail(ctx: ServiceContext) -> str:
    resp = ctx.execute(ctx.client("gmail", "v1").users().getProfile(userId="me"))
    return "profile readable" + (" (messagesTotal present)" if "messagesTotal" in resp else "")


def _calendar(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("calendar", "v3").calendarList().list(maxResults=1))
    return "calendar list readable"


def _drive(ctx: ServiceContext) -> str:
    # fields=kind avoids fetching any account data.
    ctx.execute(ctx.client("drive", "v3").about().get(fields="kind"))
    return "drive about readable"


def _docs(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("docs", "v1").documents().get(documentId=MISSING_ID))
    return "document readable"


def _sheets(ctx: ServiceContext) -> str:
    ctx.execute(
        ctx.client("sheets", "v4")
        .spreadsheets()
        .get(spreadsheetId=MISSING_ID, fields="spreadsheetId", includeGridData=False)
    )
    return "spreadsheet readable"


def _slides(ctx: ServiceContext) -> str:
    ctx.execute(
        ctx.client("slides", "v1")
        .presentations()
        .get(presentationId=MISSING_ID, fields="presentationId")
    )
    return "presentation readable"


def _forms(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("forms", "v1").forms().get(formId=MISSING_ID))
    return "form readable"


def _people(ctx: ServiceContext) -> str:
    ctx.execute(
        ctx.client("people", "v1").people().get(resourceName="people/me", personFields="metadata")
    )
    return "own profile readable"


def _meet(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("meet", "v2").conferenceRecords().list(pageSize=1))
    return "conference records readable"


def _driveactivity(ctx: ServiceContext) -> str:
    """POST is unavoidable: activity.query is the API's only endpoint and is read-only."""
    ctx.execute(ctx.client("driveactivity", "v2").activity().query(body={"pageSize": 1}))
    return "activity readable"


def _chat(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("chat", "v1").spaces().list(pageSize=1))
    return "spaces readable"


def _youtube(ctx: ServiceContext) -> str:
    resp = ctx.execute(
        ctx.client("youtube", "v3")
        .channels()
        .list(part="id", mine=True, maxResults=1, fields="items(id)")
    )
    return "channel lookup ok" + ("" if resp.get("items") else " (no channel on this account)")


def _youtube_analytics(ctx: ServiceContext) -> str:
    # groups.list has no page size; fields keeps the response to the kind tag.
    ctx.execute(ctx.client("youtubeAnalytics", "v2").groups().list(mine=True, fields="kind"))
    return "analytics groups readable"


def _youtube_reporting(ctx: ServiceContext) -> str:
    # reportTypes.list only reads the catalog of report types; it creates no job.
    ctx.execute(ctx.client("youtubereporting", "v1").reportTypes().list(pageSize=1))
    return "report types readable"


def _analytics(ctx: ServiceContext) -> str:
    ctx.execute(ctx.client("analyticsadmin", "v1beta").accountSummaries().list(pageSize=1))
    # Property 0 returns metadata common to all properties; no property id needed.
    ctx.execute(
        ctx.client("analyticsdata", "v1beta").properties().getMetadata(name="properties/0/metadata")
    )
    return "admin and data APIs readable"


def _searchconsole(ctx: ServiceContext) -> str:
    # sites.list has no page size; fields drops site URLs from the response.
    ctx.execute(ctx.client("searchconsole", "v1").sites().list(fields="siteEntry(permissionLevel)"))
    return "sites readable"


def _appsscript(ctx: ServiceContext) -> str:
    # projects.get is covered by the adapter's script.projects.readonly scope.
    ctx.execute(ctx.client("script", "v1").projects().get(scriptId=MISSING_ID, fields="scriptId"))
    return "script project readable"


def _photos(ctx: ServiceContext) -> str:
    session: Any = ctx.authorized_session()
    resp = session.get(PHOTOS_ALBUMS_URL, params={"pageSize": 1}, timeout=HTTP_TIMEOUT_SECONDS)
    if resp.status_code >= 400:
        raise _HttpFailure(int(resp.status_code), resp.content)
    return "library albums readable (Picker API not probed: it requires creating a session)"


PROBES: dict[str, Probe] = {
    "tasks": _probe("tasks", _tasks),
    "gmail": _probe("gmail", _gmail),
    "calendar": _probe("calendar", _calendar),
    "drive": _probe("drive", _drive),
    "docs": _probe("docs", _docs, not_found_ok=True),
    "sheets": _probe("sheets", _sheets, not_found_ok=True),
    "slides": _probe("slides", _slides, not_found_ok=True),
    "forms": _probe("forms", _forms, not_found_ok=True),
    "people": _probe("people", _people),
    "meet": _probe("meet", _meet, workspace_only=True),
    "driveactivity": _probe("driveactivity", _driveactivity),
    "chat": _probe("chat", _chat, workspace_only=True),
    "youtube": _probe("youtube", _youtube),
    "youtube_analytics": _probe("youtube_analytics", _youtube_analytics),
    "youtube_reporting": _probe("youtube_reporting", _youtube_reporting),
    "analytics": _probe("analytics", _analytics),
    "searchconsole": _probe("searchconsole", _searchconsole),
    "appsscript": _probe("appsscript", _appsscript, not_found_ok=True),
    "photos": _probe("photos", _photos),
}


def run_probes(ctx: ServiceContext, products: Iterable[str] | None = None) -> list[ProbeResult]:
    """Run probes for ``products`` (default: every catalog product), in order.

    Never raises: each product's failure is captured in its own result.
    """
    keys = list(products) if products is not None else list(load_catalog())
    results: list[ProbeResult] = []
    for key in keys:
        probe = PROBES.get(key)
        if probe is None:
            results.append(ProbeResult(key, False, "error", "no probe for product"))
            continue
        try:
            results.append(probe(ctx))
        except Exception as exc:  # noqa: BLE001 - defensive: one product never aborts the rest
            results.append(ProbeResult(key, False, "error", f"probe failed: {type(exc).__name__}"))
    return results
