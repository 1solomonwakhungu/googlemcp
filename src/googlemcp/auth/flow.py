"""Interactive Google account authorization, status, and revocation.

``login`` runs Google's installed-app flow: a loopback redirect on 127.0.0.1
with PKCE (S256), ``access_type=offline`` and ``prompt=consent`` so Google
always returns a refresh token. The Desktop OAuth client comes from the
secret store, or from a downloaded client JSON that is read once, copied into
the secret store, and never written anywhere else.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from googlemcp.auth.credentials import AuthError, CredentialProvider
from googlemcp.secretstore import SecretKey, SecretStore, SecretStoreError, ensure_writable

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (endpoint, not a secret)
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
TOKENINFO_URI = "https://oauth2.googleapis.com/tokeninfo"
HTTP_TIMEOUT = 15

CLIENTS_CONSOLE_URL = "https://console.cloud.google.com/auth/clients"
SETUP_DOC = "https://github.com/1solomonwakhungu/googlemcp/blob/main/docs/manual-steps.md"


def client_setup_hint() -> str:
    """Where a new user gets the Desktop OAuth client JSON and what to run next."""
    return (
        f"Create an OAuth client of type 'Desktop app' at {CLIENTS_CONSOLE_URL} "
        "(Google Auth Platform > Clients), download its JSON, then run "
        "`googlemcp auth login --client-secrets <path-to-downloaded.json>`. "
        f"See {SETUP_DOC} for the full Google Cloud setup."
    )


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str

    def as_installed(self) -> dict[str, Any]:
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://127.0.0.1", "http://localhost"],
            }
        }


def read_client_secrets(path: Path) -> ClientConfig:
    """Parse a downloaded Desktop OAuth client JSON without keeping a copy."""
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthError(f"OAuth client file {path} does not exist. {client_setup_hint()}") from None
    except ValueError:
        raise AuthError(
            f"OAuth client file {path} is not valid JSON. Download the client JSON again "
            f"from {CLIENTS_CONSOLE_URL}."
        ) from None
    except OSError as exc:
        raise AuthError(f"cannot read OAuth client file {path} ({type(exc).__name__})") from None
    if not isinstance(raw, dict) or "installed" not in raw:
        kind = next(iter(raw), "unknown") if isinstance(raw, dict) and raw else "unknown"
        raise AuthError(
            f"{path} is a {kind!r} OAuth client. `auth login` needs a Desktop app client "
            f"(JSON top-level key 'installed'). {client_setup_hint()}"
        )
    section = raw["installed"]
    client_id, client_secret = section.get("client_id"), section.get("client_secret")
    if not client_id or not client_secret:
        raise AuthError(f"{path} is missing client_id or client_secret")
    return ClientConfig(client_id=client_id, client_secret=client_secret)


def resolve_client(store: SecretStore, client_secrets: Path | None) -> ClientConfig:
    if client_secrets is not None:
        config = read_client_secrets(client_secrets)
        ensure_writable(store, SecretKey.OAUTH_CLIENT_ID)
        store.set(SecretKey.OAUTH_CLIENT_ID, config.client_id)
        store.set(SecretKey.OAUTH_CLIENT_SECRET, config.client_secret)
        return config
    client_id = store.get(SecretKey.OAUTH_CLIENT_ID)
    client_secret = store.get(SecretKey.OAUTH_CLIENT_SECRET)
    if not client_id or not client_secret:
        raise AuthError(
            f"no Desktop OAuth client in the {store.name} store. {client_setup_hint()} "
            "The JSON is read once and copied into the store; delete the file afterwards."
        )
    return ClientConfig(client_id=client_id, client_secret=client_secret)


@dataclass
class LoginResult:
    """``granted_scopes`` and ``missing_scopes`` are None when Google did not
    report the grant, so nothing can be claimed about it."""

    granted_scopes: list[str] | None
    missing_scopes: list[str] | None = None


FlowFactory = Callable[[dict[str, Any], Sequence[str]], Any]


def _default_flow_factory(client_config: dict[str, Any], scopes: Sequence[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_config(
        client_config, scopes=list(scopes), autogenerate_code_verifier=True
    )


def login(
    store: SecretStore,
    scopes: Sequence[str],
    *,
    client_secrets: Path | None = None,
    open_browser: bool = True,
    port: int = 0,
    login_hint: str | None = None,
    flow_factory: FlowFactory = _default_flow_factory,
) -> LoginResult:
    """Run the consent flow and store the resulting refresh token."""
    if not scopes:
        raise AuthError(
            "no scopes to request: enable at least one product with GOOGLEMCP_PRODUCTS or "
            "`products` in the config file. `googlemcp products` lists them."
        )
    ensure_writable(store, SecretKey.REFRESH_TOKEN)
    client = resolve_client(store, client_secrets)
    # Google's granular consent lets the user untick scopes. oauthlib would
    # raise on the narrower grant; accept it and report the gap instead.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = flow_factory(client.as_installed(), scopes)
    extra: dict[str, str] = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        extra["login_hint"] = login_hint
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
        authorization_prompt_message="Open this URL to authorize GoogleMCP:\n{url}\n",
        success_message="GoogleMCP authorization complete. You can close this tab.",
        **extra,
    )
    if not getattr(creds, "refresh_token", None):
        raise AuthError(
            "Google returned no refresh token. Remove GoogleMCP from "
            "https://myaccount.google.com/connections and run login again."
        )
    store.set(SecretKey.REFRESH_TOKEN, creds.refresh_token)
    reported = getattr(creds, "granted_scopes", None)
    if not reported:
        return LoginResult(granted_scopes=None, missing_scopes=None)
    granted = sorted(reported)
    return LoginResult(granted_scopes=granted, missing_scopes=sorted(set(scopes) - set(granted)))


@dataclass
class AuthStatus:
    backend: str
    client_configured: bool
    refresh_token_present: bool
    refresh_ok: bool | None = None
    access_token_expiry: str | None = None
    granted_scopes: list[str] = field(default_factory=list)
    # None means unknown: scopes were not checked, or Google tokeninfo failed.
    missing_scopes: list[str] | None = None
    scopes_verified: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def status(
    store: SecretStore,
    required_scopes: Sequence[str],
    *,
    check: bool = True,
    session: Any | None = None,
    provider: CredentialProvider | None = None,
) -> AuthStatus:
    """Report credential health without printing any secret."""
    try:
        client_ok = bool(store.get(SecretKey.OAUTH_CLIENT_ID)) and bool(
            store.get(SecretKey.OAUTH_CLIENT_SECRET)
        )
        token_ok = bool(store.get(SecretKey.REFRESH_TOKEN))
    except SecretStoreError as exc:
        return AuthStatus(store.name, False, False, error=str(exc))
    result = AuthStatus(store.name, client_ok, token_ok)
    if not (check and client_ok and token_ok):
        return result
    try:
        creds = (provider or CredentialProvider(store)).force_refresh()
    except AuthError as exc:
        result.refresh_ok = False
        result.error = str(exc)
        return result
    result.refresh_ok = True
    result.access_token_expiry = creds.expiry.isoformat() + "Z" if creds.expiry else None
    http = session or requests.Session()
    try:
        # POST keeps the access token out of URLs and proxy logs.
        resp = http.post(TOKENINFO_URI, data={"access_token": creds.token}, timeout=HTTP_TIMEOUT)
        info = resp.json() if resp.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        info = {}
    granted = sorted(str(info.get("scope", "")).split())
    if not granted:
        result.error = (
            "token refresh worked, but granted scopes could not be verified "
            "(Google tokeninfo unavailable); retry `googlemcp auth status`"
        )
        return result
    result.granted_scopes = granted
    result.missing_scopes = sorted(set(required_scopes) - set(granted))
    result.scopes_verified = True
    return result


def revoke(store: SecretStore, *, session: Any | None = None) -> bool:
    """Revoke the refresh token at Google, then delete it locally.

    Returns True when Google confirmed revocation. A token Google already
    considers invalid is still removed locally.
    """
    ensure_writable(store, SecretKey.REFRESH_TOKEN)
    token = store.get(SecretKey.REFRESH_TOKEN)
    if not token:
        return False
    http = session or requests.Session()
    try:
        resp = http.post(
            REVOKE_URI,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        raise AuthError(
            "could not reach Google to revoke the token; nothing was deleted. Retry, or revoke "
            "at https://myaccount.google.com/connections"
        ) from None
    confirmed = resp.status_code == 200
    if not confirmed and resp.status_code != 400:
        raise AuthError(f"Google revoke endpoint returned HTTP {resp.status_code}; token kept")
    store.delete(SecretKey.REFRESH_TOKEN)
    return confirmed
