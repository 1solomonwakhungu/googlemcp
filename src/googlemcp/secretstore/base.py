"""Secret store contract.

A secret store holds a small, fixed set of named secrets. Keys are logical
names; each backend maps them into its own namespace (keyring entries, JSON
fields, environment variables, or Secret Manager secret IDs). Values are
never logged, never included in exception messages, and never echoed by the
CLI.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class SecretKey(StrEnum):
    """Every secret GoogleMCP reads or writes: the user's Desktop OAuth client
    and refresh token for the local installed-app flow.

    The Secret Manager secret ID is ``<prefix>-<value>`` for the optional
    ``secretmanager`` backend; Terraform creates exactly these containers.
    """

    OAUTH_CLIENT_ID = "oauth-client-id"
    OAUTH_CLIENT_SECRET = "oauth-client-secret"  # noqa: S105 (name, not a value)
    REFRESH_TOKEN = "refresh-token"  # noqa: S105

    @property
    def env_var(self) -> str:
        return "GOOGLEMCP_" + self.value.upper().replace("-", "_")


class SecretStoreError(Exception):
    """A secret backend failed. The message never contains a secret value."""


@runtime_checkable
class SecretStore(Protocol):
    """Minimal key/value interface every backend implements."""

    name: str
    read_only: bool

    def get(self, key: SecretKey) -> str | None:
        """Return the current value, or None when the secret is absent."""
        ...

    def set(self, key: SecretKey, value: str) -> None:
        """Store a new value, replacing any previous one."""
        ...

    def delete(self, key: SecretKey) -> None:
        """Remove the value. Absent secrets are not an error."""
        ...


def require(store: SecretStore, key: SecretKey, hint: str) -> str:
    """Return a secret or raise an actionable error naming the missing key."""
    value = store.get(key)
    if not value:
        raise SecretStoreError(f"secret {key.value!r} is not set in the {store.name} store. {hint}")
    return value


def ensure_writable(store: SecretStore, key: SecretKey) -> None:
    if store.read_only:
        raise SecretStoreError(
            f"the {store.name} secret store is read-only; cannot write {key.value!r}"
        )
