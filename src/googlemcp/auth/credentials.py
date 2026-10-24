"""Credentials for Google API calls.

The server acts as one Google account, the one authorized with a refresh token
obtained by ``googlemcp auth login`` and kept in the secret store. Access
tokens live only in memory.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from googlemcp.redaction import redact
from googlemcp.secretstore import SecretKey, SecretStore, SecretStoreError
from googlemcp.services.base import UpstreamError

log = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (endpoint, not a secret)
# Repo-relative, not URLs: tool errors pass through redact(), which replaces URLs.
LONGEVITY_DOC = "docs/access-longevity.md in the googlemcp repository"
SETUP_DOC = "docs/manual-steps.md in the googlemcp repository"


class AuthError(UpstreamError):
    """Google credentials are missing or rejected. Message says how to recover.

    Subclasses ``UpstreamError`` so adapters and probes that catch upstream
    failures classify it as an auth problem. ``status`` is 401 unless Google
    could not be reached; ``reason`` is a stable code such as
    ``invalid_grant``, ``invalid_client``, ``missing_credentials``,
    ``refresh_failed``, or ``network``.
    """

    def __init__(self, message: str, *, reason: str = "auth_error", status: int | None = 401):
        super().__init__(message, status=status, reason=reason)


def invalid_grant_message() -> str:
    return (
        "Google rejected the stored refresh token (invalid_grant): it was revoked, expired, "
        "or replaced. Run `googlemcp auth login` again. If the OAuth consent screen "
        "publishing status is Testing, Google expires refresh tokens after 7 days; "
        f"see {LONGEVITY_DOC} for the In production fix."
    )


def map_refresh_error(exc: RefreshError) -> AuthError:
    """Translate a google-auth refresh failure into an actionable, secret-free error."""
    error_code = None
    if len(exc.args) > 1 and isinstance(exc.args[1], dict):
        error_code = exc.args[1].get("error")
    text = str(exc.args[0]) if exc.args else ""
    if error_code == "invalid_grant" or "invalid_grant" in text:
        return AuthError(invalid_grant_message(), reason="invalid_grant")
    if error_code == "invalid_client" or "invalid_client" in text:
        return AuthError(
            "Google rejected the OAuth client (invalid_client). The client was deleted or its "
            "secret rotated. Store the current Desktop client with `googlemcp auth login "
            "--client-secrets <path>`.",
            reason="invalid_client",
        )
    if error_code:
        code = redact(str(error_code))
        return AuthError(
            f"token refresh failed ({code}). Run `googlemcp auth login`.",
            reason=code,
        )
    return AuthError(
        "token refresh failed. Run `googlemcp doctor` for details.", reason="refresh_failed"
    )


def credentials_from_store(store: SecretStore) -> Credentials:
    try:
        client_id = store.get(SecretKey.OAUTH_CLIENT_ID)
        client_secret = store.get(SecretKey.OAUTH_CLIENT_SECRET)
        refresh_token = store.get(SecretKey.REFRESH_TOKEN)
    except SecretStoreError as exc:
        raise AuthError(str(exc), reason="secret_store") from None
    missing = [
        key.value
        for key, value in (
            (SecretKey.OAUTH_CLIENT_ID, client_id),
            (SecretKey.OAUTH_CLIENT_SECRET, client_secret),
            (SecretKey.REFRESH_TOKEN, refresh_token),
        )
        if not value
    ]
    if missing:
        if client_id and client_secret:
            hint = "Run `googlemcp auth login` to authorize your Google account."
        else:
            hint = (
                "Not logged in yet: run `googlemcp auth login --client-secrets "
                "<path-to-Desktop-OAuth-client.json>` once to store the OAuth client and "
                f"authorize your Google account. See {SETUP_DOC}."
            )
        raise AuthError(
            f"missing secrets in the {store.name} store: {', '.join(missing)}. {hint}",
            reason="missing_credentials",
        )
    # No scopes on refresh: Google returns a token for whatever the user
    # granted. Missing scopes surface per call (403) and in `auth status`.
    return Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
    )


class CredentialProvider:
    """Thread-safe holder that refreshes on demand and persists rotated tokens."""

    def __init__(
        self,
        store: SecretStore,
        *,
        request_factory: Callable[[], Any] = Request,
        credentials: Credentials | None = None,
    ):
        self._store = store
        self._request_factory = request_factory
        self._credentials = credentials
        self._lock = threading.Lock()

    def _load(self) -> Credentials:
        if self._credentials is None:
            self._credentials = credentials_from_store(self._store)
        return self._credentials

    def get(self) -> Credentials:
        """Return valid credentials, refreshing first when needed."""
        with self._lock:
            creds = self._load()
            if not creds.valid:
                self._refresh(creds)
            return self._load()

    def force_refresh(self) -> Credentials:
        with self._lock:
            self._refresh(self._load())
            return self._load()

    def _refresh(self, creds: Credentials, *, reloaded: bool = False) -> None:
        previous = creds.refresh_token
        try:
            creds.refresh(self._request_factory())  # type: ignore[no-untyped-call]
        except RefreshError as exc:
            error = map_refresh_error(exc)
            if not reloaded and self._store_has_newer_token(previous):
                # Someone re-ran `auth login` (or a mounted secret moved to a
                # new version) since this process loaded the token. Retry once.
                log.info("stored refresh token changed; retrying with the new one")
                self._credentials = credentials_from_store(self._store)
                self._refresh(self._credentials, reloaded=True)
                return
            raise error from None
        except TransportError:
            raise AuthError(
                "could not reach Google to refresh the access token (network error)",
                reason="network",
                status=None,
            ) from None
        if creds.refresh_token and creds.refresh_token != previous:
            self._persist_rotated(creds.refresh_token)

    def _store_has_newer_token(self, current: str | None) -> bool:
        try:
            stored = self._store.get(SecretKey.REFRESH_TOKEN)
        except SecretStoreError:
            return False
        return bool(stored) and stored != current

    def _persist_rotated(self, token: str) -> None:
        if self._store.read_only:
            log.warning(
                "Google rotated the refresh token but the %s store is read-only; "
                "the new token lives only in memory until restart",
                self._store.name,
            )
            return
        try:
            self._store.set(SecretKey.REFRESH_TOKEN, token)
            log.info("persisted rotated refresh token to the %s store", self._store.name)
        except SecretStoreError as exc:
            log.warning("could not persist rotated refresh token: %s", exc)
