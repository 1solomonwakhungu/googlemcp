"""Production ``ServiceContext``: authorized clients, retries, error mapping.

Thread safety: tools run concurrently in worker threads and httplib2 is not
thread-safe, so every ``client()`` call gets a fresh authorized HTTP object.
Adapters build their client inside each tool call, which keeps transports
unshared. Discovery documents come from the copies bundled with
google-api-python-client and are parsed once per API version.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import google_auth_httplib2
import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build_from_document
from googleapiclient.discovery_cache import get_static_doc
from googleapiclient.errors import HttpError

from googlemcp.auth.credentials import AuthError, CredentialProvider, map_refresh_error
from googlemcp.redaction import redact
from googlemcp.services.base import JSON, GoogleMCPError, UpstreamError

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 60
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
)
# 5xx and transport errors may hide a request that already took effect, so
# only methods that are safe to repeat are retried for them. 429 means the
# request was rejected before processing and is retried for every method.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
BACKOFF_BASE = 1.0
BACKOFF_CAP = 32.0
MESSAGE_LIMIT = 300


@lru_cache(maxsize=64)
def _discovery_doc(api: str, version: str) -> str:
    doc: str | None = get_static_doc(api, version)
    if doc is None:
        raise UpstreamError(f"no bundled discovery document for {api} {version}")
    return doc


class GoogleServiceContext:
    """Implements ``googlemcp.services.base.ServiceContext`` for real calls."""

    def __init__(
        self,
        provider: CredentialProvider,
        *,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        http_factory: Callable[[], httplib2.Http] | None = None,
    ):
        self._provider = provider
        self._max_retries = max_retries
        self._sleep = sleep
        self._http_factory = http_factory or (lambda: httplib2.Http(timeout=HTTP_TIMEOUT))

    def _authorized_http(self) -> google_auth_httplib2.AuthorizedHttp:
        return google_auth_httplib2.AuthorizedHttp(self._provider.get(), http=self._http_factory())

    def client(self, api: str, version: str) -> Any:
        return build_from_document(_discovery_doc(api, version), http=self._authorized_http())

    def authorized_session(self) -> Any:
        return AuthorizedSession(self._provider.get())  # type: ignore[no-untyped-call]

    def execute(self, request: Any) -> JSON:
        method = str(getattr(request, "method", "GET")).upper()
        attempt = 0
        while True:
            try:
                result: JSON = request.execute()
                return result
            except RefreshError as exc:
                raise map_refresh_error(exc) from None
            except HttpError as exc:
                status = int(exc.resp.status)
                reason, message = _error_details(exc)
                if attempt < self._max_retries and _should_retry(status, reason, method):
                    self._backoff(attempt, exc.resp)
                    attempt += 1
                    continue
                # The HttpError stays as __cause__ for logs and probes; the
                # message itself never includes the request URI.
                raise _upstream(status, reason, message) from exc
            except (OSError, httplib2.HttpLib2Error) as exc:
                if attempt < self._max_retries and method in IDEMPOTENT_METHODS:
                    self._backoff(attempt, None)
                    attempt += 1
                    continue
                raise UpstreamError(
                    f"network error calling Google ({type(exc).__name__})"
                ) from None

    def _backoff(self, attempt: int, resp: Any) -> None:
        delay = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
        delay += random.uniform(0, delay / 2)  # noqa: S311 (jitter, not crypto)
        retry_after = _retry_after(resp)
        if retry_after is not None:
            delay = min(BACKOFF_CAP, max(delay, retry_after))
        log.info("retrying Google request in %.1fs (attempt %d)", delay, attempt + 1)
        self._sleep(delay)


def _should_retry(status: int, reason: str | None, method: str) -> bool:
    if status == 429 or (status == 403 and reason in RATE_LIMIT_REASONS):
        return True
    return status in RETRYABLE_STATUS and method in IDEMPOTENT_METHODS


def _retry_after(resp: Any) -> float | None:
    if resp is None:
        return None
    value = resp.get("retry-after") if hasattr(resp, "get") else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_details(exc: HttpError) -> tuple[str | None, str | None]:
    """Extract Google's reason code and message. Never includes the request URI."""
    try:
        payload = json.loads(exc.content.decode("utf-8") if exc.content else "{}")
    except (UnicodeDecodeError, ValueError):
        return None, None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return (error if isinstance(error, str) else None), None
    # Prefer the structured ErrorInfo reason (SERVICE_DISABLED,
    # ACCESS_TOKEN_SCOPE_INSUFFICIENT, ...), then the legacy errors[0].reason.
    reason = None
    details = error.get("details")
    if isinstance(details, list):
        for item in details:
            if (
                isinstance(item, dict)
                and str(item.get("@type", "")).endswith("google.rpc.ErrorInfo")
                and item.get("reason")
            ):
                reason = item["reason"]
                break
    errors = error.get("errors")
    if reason is None and isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
    reason = reason or error.get("status")
    message = error.get("message")
    return (str(reason) if reason else None), (str(message) if message else None)


SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
DISABLED_REASONS = frozenset({"SERVICE_DISABLED", "accessNotConfigured"})
WITHHELD_STATUSES = frozenset({401, 403})


def withheld_message(status: int, reason: str | None) -> str:
    """Fixed tool-facing text for 401 and 403 responses.

    Google's free text for these statuses can quote request details, so it is
    never shown; only the status, the reason code, and a fixed hint are. The
    raw response stays available internally on the chained ``HttpError``
    (for example to ``googlemcp.probes``).
    """
    text = f"Google API error {status}"
    if reason:
        text += f" ({redact(reason)})"
    if status == 401:
        return text + ". Google rejected the credentials; run `googlemcp auth login`."
    if reason in SCOPE_REASONS:
        return text + (
            ". The stored token lacks a scope this tool needs; run `googlemcp auth login` "
            "after enabling the product or its writes."
        )
    if reason in DISABLED_REASONS:
        return text + (
            ". The Google API is not enabled for the OAuth client's project; enable it "
            "(Terraform enables catalog APIs) and retry."
        )
    return text + ". Google denied the request; details withheld."


def _upstream(status: int, reason: str | None, message: str | None) -> GoogleMCPError:
    if status == 401:
        return AuthError(withheld_message(status, reason), reason=reason or "unauthorized")
    if status in WITHHELD_STATUSES:
        return UpstreamError(withheld_message(status, reason), status=status, reason=reason)
    text = f"Google API error {status}"
    if reason:
        text += f" ({redact(reason)})"
    if message:
        # Redact before capping so a cut cannot leave half a URL behind.
        text += f": {redact(message)[:MESSAGE_LIMIT]}"
    return UpstreamError(text, status=status, reason=reason)
